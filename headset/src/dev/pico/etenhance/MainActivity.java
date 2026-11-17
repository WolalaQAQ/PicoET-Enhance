package dev.pico.etenhance;

import android.content.res.ColorStateList;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.io.File;

/** Two peer tabs; switching never changes a sharing policy or a tuning draft. */
public final class MainActivity extends ProcessingActivity {
    private final Handler cameraHandler = new Handler(Looper.getMainLooper());
    private PanelChannel channel;
    private CameraState camera;
    private Switch toggle, resultsToggle, eyesToggle;
    private Button retry, broadcastTab, tuningTab;
    private TextView status, error, resultsStatus, eyesStatus;
    private LinearLayout resultsCard, mouthRow, eyeRow;
    private View resultDivider, broadcastPage, processingPage;
    private FrameLayout pages;
    private final TextView[] clients = new TextView[3], addresses = new TextView[3];
    private String pending="", pendingAction="", issue="";
    private boolean rendering, tuningSelected;
    private long lastStatus, submitted;

    private Switch cameraSwitch(String label, String tag) {
        Switch view=new Switch(this); view.setContentDescription(s(label)); view.setMinHeight(dp(48)); view.setTag(tag);
        int[][] states={new int[]{android.R.attr.state_checked},new int[]{}};
        view.setThumbTintList(new ColorStateList(states,new int[]{ACCENT,0xffa4b0c3}));
        view.setTrackTintList(new ColorStateList(states,new int[]{0xff438e78,0xff4a566b}));
        return view;
    }

    @Override public void onCreate(Bundle bundle) {
        super.onCreate(bundle);
        channel=new PanelChannel(new File(createDeviceProtectedStorageContext().getFilesDir(),"control/camera"));
        pending=preferences.getString("camera_pending",""); pendingAction=preferences.getString("camera_action","");
        submitted=SystemClock.elapsedRealtime();
        buildScreen();
        selectTab(bundle!=null && bundle.getBoolean("tuning_tab"));
    }

    private void buildScreen() {
        LinearLayout root=column(); root.setBackgroundColor(BG);
        LinearLayout header=row(); header.setPadding(dp(24),dp(14),dp(24),dp(10));
        header.addView(text("PicoET-Enhance",22,TEXT,true),new LinearLayout.LayoutParams(0,-2,1));
        Button language=button(s("language"),false); language.setTag("language");
        language.setContentDescription(s("change_language"));
        language.setOnClickListener(v->changeLanguage()); header.addView(language); root.addView(header);
        LinearLayout tabs=row(); tabs.setPadding(dp(22),0,dp(24),dp(8));
        broadcastTab=button(s("broadcast_tab"),false); broadcastTab.setTag("broadcast_tab");
        tuningTab=button(s("tuning_tab"),false); tuningTab.setTag("tuning_tab");
        broadcastTab.setOnClickListener(v->selectTab(false)); tuningTab.setOnClickListener(v->selectTab(true));
        tabs.addView(broadcastTab,new LinearLayout.LayoutParams(0,-2,1));
        LinearLayout.LayoutParams tuningParams=new LinearLayout.LayoutParams(0,-2,1); tuningParams.leftMargin=dp(8);
        tabs.addView(tuningTab,tuningParams); root.addView(tabs);
        pages=new FrameLayout(this); root.addView(pages,new LinearLayout.LayoutParams(-1,0,1));
        broadcastPage=buildBroadcast(); pages.addView(broadcastPage,new FrameLayout.LayoutParams(-1,-1));
        setContentView(root); render();
    }

    private void selectTab(boolean tuning) {
        tuningSelected=tuning;
        if(tuning && processingPage==null) {
            processingPage=tuningPanel(); pages.addView(processingPage,new FrameLayout.LayoutParams(-1,-1));
        }
        broadcastPage.setVisibility(tuning?View.GONE:View.VISIBLE);
        if(processingPage!=null) processingPage.setVisibility(tuning?View.VISIBLE:View.GONE);
        styleTab(broadcastTab,!tuning); styleTab(tuningTab,tuning);
        if(!tuning) lastStatus=0; // Refresh on return without changing any sharing policy.
    }

    private void styleTab(Button tab, boolean active) {
        tab.setSelected(active);
        if(active) selected(tab);
        else { surface(tab,CARD,LINE); tab.setTextColor(TEXT); }
    }

    private View buildBroadcast() {
        ScrollView scroll=new ScrollView(this); scroll.setClipToPadding(false); scroll.setPadding(dp(22),dp(12),dp(24),dp(20));
        LinearLayout content=column(); scroll.addView(content);

        LinearLayout transport=card(content);
        toggle=cameraSwitch("camera_title","camera_toggle");
        toggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-on":"camera-off");});
        status=text("",14,ACCENT,true); status.setTag("camera_state");
        addSwitchRow(transport,"camera_title",toggle,status);
        gap(transport,8); transport.addView(text(s("camera_privacy"),13,AMBER,false));
        error=text("",13,0xffffa7a7,false); error.setTextIsSelectable(true); error.setTag("camera_error"); transport.addView(error);
        retry=button("",false); retry.setTag("camera_retry");
        retry.setOnClickListener(v->request(camera.enabled?"camera-on":"camera-off")); transport.addView(retry);

        resultsCard=card(content); resultsCard.setTag("camera_results_group");
        resultsCard.addView(text(s("camera_results_heading"),18,TEXT,true));
        gap(resultsCard,6); resultsCard.addView(text(s("camera_results_hint"),14,MUTED,false));
        resultsToggle=cameraSwitch("camera_results_title","camera_results_toggle");
        resultsToggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-results-on":"camera-results-off");});
        resultsStatus=text("",13,MUTED,false);
        mouthRow=addSwitchRow(resultsCard,"camera_results_title",resultsToggle,resultsStatus);
        resultDivider=new View(this); resultDivider.setBackgroundColor(LINE);
        resultsCard.addView(resultDivider,new LinearLayout.LayoutParams(-1,dp(1)));
        eyesToggle=cameraSwitch("camera_eyes_title","camera_eyes_toggle");
        eyesToggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-eyes-on":"camera-eyes-off");});
        eyesStatus=text("",13,MUTED,false);
        eyeRow=addSwitchRow(resultsCard,"camera_eyes_title",eyesToggle,eyesStatus);

        LinearLayout cameras=card(content);
        cameras.addView(text(s("camera_images_heading"),18,TEXT,true)); gap(cameras,10);
        boolean wide=widthDp()>=760;
        LinearLayout streams=wide?row():column(); streams.setGravity(Gravity.TOP); cameras.addView(streams);
        for(int i:new int[]{1,2,0}) {
            String name=CameraState.STREAMS[i];
            LinearLayout lane=column();
            lane.addView(text(s("camera_"+name),16,TEXT,true)); gap(lane,4);
            clients[i]=text("",13,MUTED,false); clients[i].setTag("camera_clients_"+name); lane.addView(clients[i]);
            addresses[i]=text("",13,TECH,false); addresses[i].setTextIsSelectable(true); addresses[i].setTag("camera_url_"+name);
            lane.addView(addresses[i]);
            LinearLayout.LayoutParams params=wide?new LinearLayout.LayoutParams(0,-2,1):new LinearLayout.LayoutParams(-1,-2);
            if(i!=0) { if(wide) params.rightMargin=dp(12); else params.bottomMargin=dp(12); }
            streams.addView(lane,params);
        }
        return scroll;
    }

    private LinearLayout addSwitchRow(LinearLayout parent,String label,Switch control,TextView state) {
        LinearLayout item=row(); item.setPadding(0,dp(4),0,dp(4));
        LinearLayout copy=column();
        copy.addView(text(s(label),16,TEXT,true)); gap(copy,4); copy.addView(state);
        item.addView(copy,new LinearLayout.LayoutParams(0,-2,1)); item.addView(control);
        parent.addView(item,new LinearLayout.LayoutParams(-1,-2));
        return item;
    }

    private void render() {
        rendering=true;
        toggle.setChecked(camera!=null && camera.enabled); toggle.setEnabled(camera!=null && pending.isEmpty());
        String phase=pendingAction.equals("camera-on")?"starting":pendingAction.equals("camera-off")?"stopping":camera==null?"loading":camera.state;
        status.setText(s("camera_state_"+phase));
        boolean supportsResults=camera!=null && camera.resultsSupported;
        boolean supportsEyes=camera!=null && camera.eyesSupported;
        resultsCard.setVisibility(supportsResults||supportsEyes?View.VISIBLE:View.GONE);
        mouthRow.setVisibility(supportsResults?View.VISIBLE:View.GONE);
        eyeRow.setVisibility(supportsEyes?View.VISIBLE:View.GONE);
        resultDivider.setVisibility(supportsResults&&supportsEyes?View.VISIBLE:View.GONE);
        resultsToggle.setChecked(supportsResults && camera.resultsEnabled);
        resultsToggle.setEnabled(supportsResults && pending.isEmpty());
        if(supportsResults) {
            String resultKey=pendingAction.startsWith("camera-results-")?"updating":
                camera.resultsOnline?(camera.resultsEnabled?"on":"pending_off"):camera.resultsEnabled?"pending_on":"off";
            resultsStatus.setText(s("camera_results_"+resultKey).replace("{0}",String.valueOf(camera.resultsClients)));
        }
        eyesToggle.setChecked(supportsEyes && camera.eyesEnabled);
        eyesToggle.setEnabled(supportsEyes && pending.isEmpty());
        if(supportsEyes) {
            String resultKey=pendingAction.startsWith("camera-eyes-")?"updating":
                camera.eyesOnline?(camera.eyesEnabled?"on":"pending_off"):camera.eyesEnabled?"pending_on":"off";
            eyesStatus.setText(s("camera_results_"+resultKey).replace("{0}",String.valueOf(camera.eyesClients)));
        }
        for(int i=0;i<3;++i) {
            clients[i].setText(camera==null?"—":s("camera_clients").replace("{0}",String.valueOf(camera.clients[i])));
            addresses[i].setText(camera!=null && camera.online && !camera.address.isEmpty()
                    ? "http://"+camera.address+":"+(45681+i)+"/stream.mjpg" : s("camera_url_unavailable"));
        }
        String detail=!issue.isEmpty()?issue:camera==null?"":camera.error;
        if(!pending.isEmpty() && SystemClock.elapsedRealtime()-submitted>15000)
            detail=pendingAction.equals("camera-status")?"worker_unavailable":"operation_wait";
        error.setText(id("camera_error_"+detail)!=0?s("camera_error_"+detail):detail); error.setVisibility(detail.isEmpty()?View.GONE:View.VISIBLE);
        retry.setVisibility(camera!=null && camera.state.equals("error")?View.VISIBLE:View.GONE);
        retry.setEnabled(pending.isEmpty());
        retry.setText(s(camera!=null && camera.enabled?"camera_retry_on":"camera_retry_off"));
        rendering=false;
    }

    private void request(String action) {
        if(!pending.isEmpty()) { render(); return; }
        try {
            pending=channel.submit(action,""); pendingAction=action; submitted=SystemClock.elapsedRealtime();
            preferences.edit().putString("camera_pending",pending).putString("camera_action",action).apply();
        } catch(Exception exception) { issue=exception.getMessage(); }
        render();
    }

    private final Runnable cameraPoll=new Runnable() {
        @Override public void run() {
            try {
                if(!pending.isEmpty()) {
                    String[] reply=channel.response(pending);
                    if(reply!=null) {
                        pending=""; pendingAction=""; lastStatus=SystemClock.elapsedRealtime();
                        preferences.edit().remove("camera_pending").remove("camera_action").apply();
                        try { camera=CameraState.parse(reply[1]); issue=""; }
                        catch(RuntimeException invalid) { camera=null; issue="worker_unavailable"; }
                    }
                    render();
                } else if(!tuningSelected && SystemClock.elapsedRealtime()-lastStatus>=2000) request("camera-status");
            } catch(Exception exception) { error.setText(exception.getMessage()); error.setVisibility(View.VISIBLE); }
            cameraHandler.postDelayed(this,500);
        }
    };
    @Override protected void onSaveInstanceState(Bundle bundle) {
        bundle.putBoolean("tuning_tab",tuningSelected); super.onSaveInstanceState(bundle);
    }
    @Override protected void onResume() { super.onResume(); lastStatus=0; cameraHandler.post(cameraPoll); }
    @Override protected void onPause() { cameraHandler.removeCallbacks(cameraPoll); super.onPause(); }
}
