package dev.pico.etenhance;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.UUID;

/** Only sandbox file IPC: no process execution, root request or network access. */
final class PanelChannel {
    private final File directory;
    PanelChannel(File directory) { this.directory = directory; directory.mkdirs(); }
    static String encode(String id, String action, String configuration) throws IOException {
        switch (action) {
            case "read": case "defaults": case "balanced": case "responsive": case "raw": case "apply":
            case "camera-status": case "camera-on": case "camera-off":
            case "camera-results-on": case "camera-results-off":
            case "camera-eyes-on": case "camera-eyes-off": configuration = ""; break;
            case "write": case "check": break;
            default: throw new IOException("Unknown action");
        }
        if (configuration.getBytes(StandardCharsets.UTF_8).length > 16384 || configuration.indexOf('\0') >= 0)
            throw new IOException("Configuration exceeds the protocol limits");
        if (!id.matches("[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")) throw new IOException("Invalid request identifier");
        return "PPC1\n" + id + "\n" + action + "\n" + configuration;
    }
    String submit(String action, String configuration) throws IOException {
        final String id = UUID.randomUUID().toString();
        File next = new File(directory, "request.new");
        Files.write(next.toPath(), encode(id, action, configuration).getBytes(StandardCharsets.UTF_8));
        Files.move(next.toPath(), new File(directory, "request.txt").toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        return id;
    }
    String[] response(String id) throws IOException {
        File file = new File(directory, "reply.txt");
        if (!file.isFile()) return null;
        String[] parts = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8).split("\n", 4);
        if (parts.length != 4 || !parts[0].equals("PPR1") || !parts[1].equals(id)) return null;
        return new String[]{parts[2], parts[3]};
    }
}
