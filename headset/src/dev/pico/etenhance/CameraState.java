package dev.pico.etenhance;

import java.util.*;

/** Actual module response, never an optimistic preference for the switch. */
final class CameraState {
    static final String[] STREAMS = {"mouth", "eye_left", "eye_right"};
    final boolean enabled, online;
    final boolean resultsSupported, resultsEnabled, resultsOnline;
    final int resultsClients;
    final boolean eyesSupported, eyesEnabled, eyesOnline;
    final int eyesClients;
    final String state, address, error;
    final int[] clients;

    private CameraState(boolean enabled, boolean online, String state, String address, String error, int[] clients,
                        Map<String, String> values) {
        this.enabled = enabled; this.online = online; this.state = state;
        this.address = address; this.error = error; this.clients = clients;
        resultsSupported = values.containsKey("results.enabled");
        resultsEnabled = resultsSupported && bit(values.get("results.enabled"));
        resultsOnline = resultsSupported && bit(values.get("results.online"));
        resultsClients = resultsSupported && bit(values.get("results.clients")) ? 1 : 0;
        eyesSupported = values.containsKey("eyes.enabled");
        eyesEnabled = eyesSupported && bit(values.get("eyes.enabled"));
        eyesOnline = eyesSupported && bit(values.get("eyes.online"));
        eyesClients = eyesSupported && bit(values.get("eyes.clients")) ? 1 : 0;
    }

    static CameraState parse(String text) {
        Map<String, String> values = new HashMap<>();
        for (String line : text.split("\n")) {
            int split = line.indexOf('=');
            if (split <= 0) throw new IllegalArgumentException("Invalid camera status");
            String key = line.substring(0, split);
            if (values.put(key, line.substring(split + 1)) != null)
                throw new IllegalArgumentException("Duplicate camera status");
        }
        if (!"1".equals(values.get("version"))) throw new IllegalArgumentException("Unsupported camera status");
        String state = values.get("state");
        if (!Arrays.asList("off", "idle", "connected", "waiting", "error").contains(state))
            throw new IllegalArgumentException("Unknown camera state");
        int[] clients = new int[STREAMS.length];
        for (int i = 0; i < clients.length; ++i) {
            clients[i] = Integer.parseInt(values.get(STREAMS[i] + ".clients"));
            if (clients[i] < 0 || clients[i] > 8) throw new IllegalArgumentException("Invalid camera client count");
        }
        return new CameraState(bit(values.get("enabled")), bit(values.get("online")), state,
                Objects.requireNonNull(values.get("address")), Objects.requireNonNull(values.get("error")), clients, values);
    }

    private static boolean bit(String value) {
        if (!"0".equals(value) && !"1".equals(value)) throw new IllegalArgumentException("Invalid camera flag");
        return "1".equals(value);
    }
}
