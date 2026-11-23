package net.onlinewatch;

import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.HexFormat;
import java.util.Locale;
import java.util.Properties;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ServerData;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Online Watch: once per join on Hypixel (a rejoin is a new join; switching lobbies is not) this
 * sends a signed code to a hidden Discord channel. The Online Watch bot checks every new login it
 * sees on Hypixel against it: no valid code means someone else logged in with this account.
 *
 * The webhook, message id and key are built into the jar by the GitHub workflow
 * (onlinewatch.properties); the jar is only handed out in the hidden channel.
 */
public class OnlineWatch implements ClientModInitializer {
	private static final Logger LOG = LoggerFactory.getLogger("onlinewatch");
	private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
	private static final SecureRandom RANDOM = new SecureRandom();

	private String webhook;
	private String message;
	private byte[] key;
	private String player;

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
		key = HexFormat.of().parseHex(p.getProperty("key", ""));
		player = p.getProperty("player", "");
		if (webhook.isEmpty() || message.isEmpty() || key.length == 0) {
			LOG.warn("Online Watch: settings incomplete, nothing will be sent");
			return;
		}
		// JOIN fires once per connection to a server: Hypixel lobby switches stay on the same one
		ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> onJoin(client));
		LOG.info("Online Watch ready for {}", player);
	}

	private void onJoin(Minecraft client) {
		ServerData server = client.getCurrentServer();
		if (server == null || !server.ip.toLowerCase(Locale.ROOT).contains("hypixel.net")) {
			return;
		}
		UUID uuid = client.getUser().getProfileId();
		String name = client.getUser().getName();
		if (!player.isEmpty() && !player.equalsIgnoreCase(name)) {
			LOG.info("Online Watch: built for {}, not sending for {}", player, name);
			return;
		}
		long time = System.currentTimeMillis();
		byte[] nonce = new byte[12];
		RANDOM.nextBytes(nonce);
		String n = HexFormat.of().formatHex(nonce);
		String id = uuid.toString().replace("-", "");
		String sig = sign(name + "|" + id + "|" + time + "|" + n);
		String code = "{\"p\":\"" + name + "\",\"u\":\"" + id + "\",\"t\":" + time + ",\"n\":\"" + n
				+ "\",\"sig\":\"" + sig + "\"}";
		// the code is the whole content of the message the bot reads
		String body = "{\"content\":\"" + code.replace("\\", "\\\\").replace("\"", "\\\"") + "\"}";
		send(body, 3);
	}

	private String sign(String text) {
		try {
			Mac mac = Mac.getInstance("HmacSHA256");
			mac.init(new SecretKeySpec(key, "HmacSHA256"));
			return HexFormat.of().formatHex(mac.doFinal(text.getBytes(StandardCharsets.UTF_8)));
		} catch (Exception e) {
			throw new IllegalStateException(e);
		}
	}

	private void send(String body, int tries) {
		HttpRequest req = HttpRequest.newBuilder(URI.create(webhook + "/messages/" + message))
				.timeout(Duration.ofSeconds(15))
				.header("Content-Type", "application/json")
				.header("User-Agent", "online-watch-mod/1.0")
				.method("PATCH", HttpRequest.BodyPublishers.ofString(body))
				.build();
		HTTP.sendAsync(req, HttpResponse.BodyHandlers.discarding()).whenComplete((res, err) -> {
			if (err == null && res.statusCode() / 100 == 2) {
				LOG.info("Online Watch: join code sent");
			} else if (tries > 1) {
				LOG.warn("Online Watch: sending failed ({}), trying again", err != null ? err.toString() : res.statusCode());
				try {
					Thread.sleep(5000);
				} catch (InterruptedException ignored) {
					Thread.currentThread().interrupt();
				}
				send(body, tries - 1);
			} else {
				LOG.warn("Online Watch: could not send the join code");
			}
		});
	}
}
