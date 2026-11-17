package dev.pico.etenhance;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;

public final class PanelChannelTest {
    static int checks;
    static void check(boolean condition) { ++checks; if (!condition) throw new AssertionError(); }
    public static void main(String[] args) throws Exception {
        String id = "12345678-abcd-1234-abcd-123456789abc";
        for (String action : new String[]{"read", "defaults", "balanced", "responsive", "raw", "apply", "camera-status", "camera-on", "camera-off"})
            check(PanelChannel.encode(id, action, "ignored").equals("PPC1\n"+id+"\n"+action+"\n"));
        for (String action : new String[]{"write", "check"})
            check(PanelChannel.encode(id, action, "version=1\neye.enabled=1\n").endsWith("\nversion=1\neye.enabled=1\n"));
        for (String action : new String[]{"sh", "read; id", "write\napply", "--force"}) {
            try { PanelChannel.encode(id, action, ""); throw new AssertionError(); } catch (IOException expected) { ++checks; }
        }
        try { PanelChannel.encode("../bad", "read", ""); throw new AssertionError(); } catch (IOException expected) { ++checks; }
        try { PanelChannel.encode(id, "write", new String(new char[16385]).replace('\0', 'a')); throw new AssertionError(); } catch (IOException expected) { ++checks; }
        try { PanelChannel.encode(id, "write", "version=1\0"); throw new AssertionError(); } catch (IOException expected) { ++checks; }
        File directory = new File(args[0]); directory.mkdirs(); PanelChannel channel = new PanelChannel(directory);
        String request = channel.submit("write", "version=1\n# 中文\n");
        String wire = new String(Files.readAllBytes(new File(directory, "request.txt").toPath()), StandardCharsets.UTF_8);
        check(wire.equals("PPC1\n"+request+"\nwrite\nversion=1\n# 中文\n"));
        check(channel.response(request) == null);
        File reply = new File(directory, "reply.txt");
        Files.write(reply.toPath(), ("PPR1\n"+id+"\n0\nstale").getBytes(StandardCharsets.UTF_8));
        check(channel.response(request) == null);
        Files.write(reply.toPath(), ("PPR1\n"+request+"\n0\nversion=1\n").getBytes(StandardCharsets.UTF_8));
        check(channel.response(request)[1].equals("version=1\n"));
        PanelChannel camera=new PanelChannel(new File(directory,"camera"));
        String cameraRequest=camera.submit("camera-on","");
        check(new String(Files.readAllBytes(new File(directory,"request.txt").toPath()),StandardCharsets.UTF_8).equals(wire));
        check(new File(directory,"camera/request.txt").isFile());
        check(camera.response(cameraRequest)==null);
        check(channel.response(request)[1].equals("version=1\n"));
        System.out.println(checks+" rootless panel channel checks passed");
    }
}
