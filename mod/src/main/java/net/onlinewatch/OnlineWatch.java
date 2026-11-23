package net.onlinewatch;

import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ServerData;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Online Watch: one mod for every watched account. Whoever plays with it, on whichever of the
 * watched accounts, sends a signed code for that account into ONE shared message in a hidden
 * Discord channel: once per join on Hypixel (a rejoin is a new join; switching lobbies is not) and
 * once when leaving (leaving the server or closing the game). The Online Watch bot checks every new
 * login it sees on Hypixel against the join code (no valid code: someone else logged in with this
 * account) and counts a leave at once when the leave code is there.
 *
 * The message content is {"<account>": {"join": code, "leave": code}, ...}: the mod reads it,
 * changes only its own account's entry and writes it back, so that the accounts never overwrite
 * each other. The leave keeps the join code next to it.
 *
 * The webhook, the message id and every account's key are built into the jar by the GitHub
 * workflow (onlinewatch.properties); the jar is only handed out in the hidden channel.
 */
public class OnlineWatch implements ClientModInitializer {
	private static final Logger LOG = LoggerFactory.getLogger("onlinewatch");
	private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
	private static final SecureRandom RANDOM = new SecureRandom();

	private String webhook;
	private String message;
	private final Map<String, byte[]> keys = new HashMap<>();

	// the account on Hypixel right now (null when not connected there) and its join code
	private String current;
	private String currentId;
	private String joinCode;

	@Override
	public void onInitializeClient() {
		Properties p = new Properties();
		try (InputStream in = OnlineWatch.class.getResourceAsStream("/onlinewatch.properties")) {
			if (in == null) {
				LOG.warn("Online Watch: no onlinewatch.properties in the jar, nothing will be sent");
				return;
			}
			p.load(in);
		} catch (Exception e) {
			LOG.warn("Online Watch: could not read its settings", e);
			return;
		}
		webhook = p.getProperty("webhook", "").split("\\?")[0];
		message = p.getProperty("message", "");
		for (String name : p.getProperty("players", "").split(",")) {
			String n = name.trim().toLowerCase(Locale.ROOT);
			String key = p.getProperty("key." + n, "");
			if (!n.isEmpty() && !key.isEmpty()) {
				keys.put(n, HexFormat.of().parseHex(key));
			}
		}
		if (webhook.isEmpty() || message.isEmpty() || keys.isEmpty()) {
			LOG.warn("Online Watch: settings incomplete, nothing will be sent");
			return;
		}
		// JOIN fires once per connection to a server: Hypixel lobby switches stay on the same one
		ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> onJoin(client));
		// the leave code as early as possible, on whichever comes first: leaving the server,
		// the game closing, or Java shutting down (the others then find nothing left to send)
		ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> onLeave());
		ClientLifecycleEvents.CLIENT_STOPPING.register(client -> onLeave());
		Runtime.getRuntime().addShutdownHook(new Thread(this::onLeave, "onlinewatch-leave"));
		LOG.info("Online Watch ready for {} account(s)", keys.size());
	}

	private void onJoin(Minecraft client) {
		ServerData server = client.getCurrentServer();
		if (server == null || !server.ip.toLowerCase(Locale.ROOT).contains("hypixel.net")) {
			return;
		}
		String name = client.getUser().getName();
		if (!keys.containsKey(name.toLowerCase(Locale.ROOT))) {
			LOG.info("Online Watch: {} is not one of the watched accounts, nothing sent", name);
			return;
		}
		UUID uuid = client.getUser().getProfileId();
		JsonObject entry = new JsonObject();
		synchronized (this) {
			current = name;
			currentId = uuid.toString().replace("-", "");
			joinCode = code(name, currentId, null);
			entry.add("join", JsonParser.parseString(joinCode));
		}
		// in the background: joining never waits for Discord
		CompletableFuture.runAsync(() -> {
			for (int tries = 3; tries > 0; tries--) {
				String failed = write(name, entry, 15);
				if (failed == null) {
					LOG.info("Online Watch: join code sent");
					return;
				}
				LOG.warn("Online Watch: sending the join code failed ({}){}", failed, tries > 1 ? ", trying again" : "");
				sleep(5000);
			}
		});
	}

	private void onLeave() {
		String name;
		JsonObject entry = new JsonObject();
		synchronized (this) { // once per session, whichever hook comes first
			name = current;
			if (name == null) {
				return; // not on Hypixel with a watched account, or already sent
			}
			current = null;
			entry.add("join", JsonParser.parseString(joinCode));
			entry.add("leave", JsonParser.parseString(code(name, currentId, "leave")));
		}
		// right away and waiting for it (at most a few seconds): when the game is being closed,
		// a request in the background would die with it
		String failed = write(name, entry, 4);
		if (failed == null) {
			LOG.info("Online Watch: leave code sent");
			return;
		}
		LOG.warn("Online Watch: leave code not sent ({}), trying again in the background", failed);
		// the game is still running (left the server, not closed): two more tries
		CompletableFuture.runAsync(() -> {
			for (int tries = 2; tries > 0; tries--) {
				sleep(5000);
				String again = write(name, entry, 15);
				if (again == null) {
					LOG.info("Online Watch: leave code sent");
					return;
				}
				LOG.warn("Online Watch: sending the leave code failed ({})", again);
			}
		});
	}

	/**
	 * Puts the account's entry into the shared message: reads the message, changes only this
	 * account's entry, writes it back. Returns null when done, else why not (Discord's answer).
	 */
	private String write(String name, JsonObject entry, int timeoutSeconds) {
		String url = webhook + "/messages/" + message;
		try {
			HttpResponse<String> got = HTTP.send(HttpRequest.newBuilder(URI.create(url))
					.timeout(Duration.ofSeconds(timeoutSeconds))
					.header("User-Agent", "online-watch-mod/1.2")
					.GET().build(), HttpResponse.BodyHandlers.ofString());
			if (got.statusCode() / 100 != 2) {
				return why(got);
			}
			JsonObject all = new JsonObject();
			try {
				String content = JsonParser.parseString(got.body()).getAsJsonObject().get("content").getAsString();
				JsonElement parsed = JsonParser.parseString(content);
				if (parsed.isJsonObject()) {
					all = parsed.getAsJsonObject();
				}
			} catch (Exception e) {
				// empty or not JSON yet: start with this account only
			}
			all.add(name.toLowerCase(Locale.ROOT), entry);
			JsonObject body = new JsonObject();
			body.addProperty("content", all.toString());
			HttpResponse<String> put = HTTP.send(HttpRequest.newBuilder(URI.create(url))
					.timeout(Duration.ofSeconds(timeoutSeconds))
					.header("Content-Type", "application/json")
					.header("User-Agent", "online-watch-mod/1.2")
					.method("PATCH", HttpRequest.BodyPublishers.ofString(body.toString()))
					.build(), HttpResponse.BodyHandlers.ofString());
			return put.statusCode() / 100 == 2 ? null : why(put);
		} catch (Exception e) {
			return e.toString();
		}
	}

	/** Status and Discord's own reason (e.g. "Unknown Message"), plus which message was meant. */
	private String why(HttpResponse<String> res) {
		String reason = res.body() == null ? "" : res.body().replaceAll("\\s+", " ");
		if (reason.length() > 200) {
			reason = reason.substring(0, 200);
		}
		return res.statusCode() + " " + reason + " - message " + message;
	}

	/** A signed code: name, uuid, time, a random nonce and (for a leave) the event. */
	private String code(String name, String id, String event) {
		long time = System.currentTimeMillis();
		byte[] nonce = new byte[12];
		RANDOM.nextBytes(nonce);
		String n = HexFormat.of().formatHex(nonce);
		String text = name + "|" + id + "|" + time + "|" + n + (event == null ? "" : "|" + event);
		String sig = sign(keys.get(name.toLowerCase(Locale.ROOT)), text);
		return "{\"p\":\"" + name + "\",\"u\":\"" + id + "\",\"t\":" + time + ",\"n\":\"" + n + "\""
				+ (event == null ? "" : ",\"e\":\"" + event + "\"") + ",\"sig\":\"" + sig + "\"}";
	}

	private static String sign(byte[] key, String text) {
		try {
			Mac mac = Mac.getInstance("HmacSHA256");
			mac.init(new SecretKeySpec(key, "HmacSHA256"));
			return HexFormat.of().formatHex(mac.doFinal(text.getBytes(StandardCharsets.UTF_8)));
		} catch (Exception e) {
			throw new IllegalStateException(e);
		}
	}

	private static void sleep(long ms) {
		try {
			Thread.sleep(ms);
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
		}
	}
}
