package dev.pico.etenhance;

public final class CameraStateTest {
    static int checks;
    static void check(boolean value){++checks;if(!value)throw new AssertionError();}
    public static void main(String[] args) {
        String base="version=1\nenabled=1\nonline=1\nstate=idle\naddress=192.0.2.1\nerror=\nmouth.clients=0\neye_left.clients=0\neye_right.clients=0\n";
        CameraState idle=CameraState.parse(base);check(idle.enabled&&idle.online&&idle.clients[0]==0);
        check(!idle.resultsSupported);
        CameraState results=CameraState.parse(base+"results.enabled=1\nresults.online=1\nresults.clients=1\n");
        check(results.resultsSupported&&results.resultsEnabled&&results.resultsOnline&&results.resultsClients==1);
        check(!results.eyesSupported);
        CameraState both=CameraState.parse(base+"results.enabled=0\nresults.online=0\nresults.clients=0\neyes.enabled=1\neyes.online=1\neyes.clients=1\n");
        check(!both.resultsEnabled&&both.eyesSupported&&both.eyesEnabled&&both.eyesOnline&&both.eyesClients==1);
        check(CameraState.parse(base.replace("state=idle","state=connected").replace("mouth.clients=0","mouth.clients=8")).clients[0]==8);
        CameraState failed=CameraState.parse(base.replace("enabled=1","enabled=0").replace("state=idle","state=error").replace("error=\n","error=stop_pending\n"));
        check(!failed.enabled&&failed.online&&failed.error.equals("stop_pending"));
        for(String bad:new String[]{base.replace("version=1","version=2"),base+"enabled=1\n",base.replace("online=1","online=yes"),
                base.replace("state=idle","state=pretend"),base.replace("mouth.clients=0","mouth.clients=9"),base.replace("mouth.clients=0","mouth.clients=-1")}) {
            try{CameraState.parse(bad);throw new AssertionError();}catch(IllegalArgumentException expected){++checks;}
        }
        System.out.println(checks+" camera state checks passed");
    }
}
