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

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ServerData;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Online Watch: one mod for every watched account. Whoever plays with it, on whichever of the
 * watched accounts, sends a signed code to that account's message in a hidden Discord channel:
 * once per join on Hypixel (a rejoin is a new join; switching lobbies is not) and once when
 * leaving. The Online Watch bot checks every new login it sees on Hypixel against the join code
 * (no valid code: someone else logged in with this account) and counts a leave at once when the
 * leave code is there.
 *
 * The message content is {"join": code, "leave": code or absent}: the leave keeps the join code
 * next to it, so the bot can still check the join when it sees both in the same round.
 *
 * The webhook and, per account, the message id and key are built into the jar by the GitHub
 * workflow (onlinewatch.properties); the jar is only handed out in the hidden channel.
 */
public class OnlineWatch implements ClientModInitializer {
	private static final Logger LOG = LoggerFactory.getLogger("onlinewatch");
	private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
	private static final SecureRandom RANDOM = new SecureRandom();

	private record Account(String message, byte[] key) {
	}

	private String webhook;
	private final Map<String, Account> accounts = new HashMap<>();

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
		for (String name : p.getProperty("players", "").split(",")) {
			String n = name.trim().toLowerCase(Locale.ROOT);
			String message = p.getProperty("message." + n, "");
			String key = p.getProperty("key." + n, "");
			if (!n.isEmpty() && !message.isEmpty() && !key.isEmpty()) {
				accounts.put(n, new Account(message, HexFormat.of().parseHex(key)));
			}
		}
		if (webhook.isEmpty() || accounts.isEmpty()) {
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
		LOG.info("Online Watch ready for {} account(s)", accounts.size());
	}

	private void onJoin(Minecraft client) {
		ServerData server = client.getCurrentServer();
		if (server == null || !server.ip.toLowerCase(Locale.ROOT).contains("hypixel.net")) {
			return;
		}
		String name = client.getUser().getName();
		if (!accounts.containsKey(name.toLowerCase(Locale.ROOT))) {
			LOG.info("Online Watch: {} is not one of the watched accounts, nothing sent", name);
			return;
		}
		UUID uuid = client.getUser().getProfileId();
		current = name;
		currentId = uuid.toString().replace("-", "");
		joinCode = code(name, currentId, null);
		// async: joining never waits for Discord
		sendAsync(name, "{\"join\":" + joinCode + "}", 3);
	}

	private void onLeave() {
		String name;
		String body;
		synchronized (this) { // once per session, whichever hook comes first
			name = current;
			if (name == null) {
				return; // not on Hypixel with a watched account, or already sent
			}
			current = null;
			body = "{\"join\":" + joinCode + ",\"leave\":" + code(name, currentId, "leave") + "}";
		}
		// right away and waiting for it (at most a few seconds): when the game is being closed,
		// an async request would die with it
		try {
			HttpResponse<Void> res = HTTP.send(request(name, body, 4), HttpResponse.BodyHandlers.discarding());
			if (res.statusCode() / 100 == 2) {
				LOG.info("Online Watch: leave code sent");
				return;
			}
			LOG.warn("Online Watch: leave code not sent ({})", res.statusCode());
		} catch (Exception e) {
			LOG.warn("Online Watch: leave code not sent ({})", e.toString());
		}
	}

	/** A signed code: name, uuid, time, a random nonce and (for a leave) the event. */
	private String code(String name, String id, String event) {
		long time = System.currentTimeMillis();
		byte[] nonce = new byte[12];
		RANDOM.nextBytes(nonce);
		String n = HexFormat.of().formatHex(nonce);
		String text = name + "|" + id + "|" + time + "|" + n + (event == null ? "" : "|" + event);
		String sig = sign(accounts.get(name.toLowerCase(Locale.ROOT)).key(), text);
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

	private HttpRequest request(String name, String content, int timeoutSeconds) {
		// the codes are the whole content of the account's message, which the bot reads
		String body = "{\"content\":\"" + content.replace("\\", "\\\\").replace("\"", "\\\"") + "\"}";
		String message = accounts.get(name.toLowerCase(Locale.ROOT)).message();
		return HttpRequest.newBuilder(URI.create(webhook + "/messages/" + message))
				.timeout(Duration.ofSeconds(timeoutSeconds))
				.header("Content-Type", "application/json")
				.header("User-Agent", "online-watch-mod/1.1")
				.method("PATCH", HttpRequest.BodyPublishers.ofString(body))
				.build();
	}

	private void sendAsync(String name, String content, int tries) {
		HTTP.sendAsync(request(name, content, 15), HttpResponse.BodyHandlers.discarding()).whenComplete((res, err) -> {
			if (err == null && res.statusCode() / 100 == 2) {
				LOG.info("Online Watch: join code sent");
			} else if (tries > 1) {
				LOG.warn("Online Watch: sending failed ({}), trying again", err != null ? err.toString() : res.statusCode());
				try {
					Thread.sleep(5000);
				} catch (InterruptedException ignored) {
					Thread.currentThread().interrupt();
				}
				sendAsync(name, content, tries - 1);
			} else {
				LOG.warn("Online Watch: could not send the join code");
			}
		});
	}
}
