package dev.pico.etenhance;

import android.app.Activity;
import android.content.*;
import android.content.res.*;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.io.File;
import java.util.Locale;

/** Companion home: independent camera transport and the existing tuning panel. */
public final class MainActivity extends Activity {
    private static final int BG=0xff10151e, CARD=0xff1b2533, LINE=0xff344357;
    private static final int TEXT=0xfff3f6fc, MUTED=0xffa9b8ce, ACCENT=0xff79e1c2, INK=0xff10251e;
    private static final int TECH=0xff9fc2ff;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private SharedPreferences preferences;
    private Context localized;
    private PanelChannel channel;
    private CameraState camera;
    private Switch toggle, resultsToggle, eyesToggle;
    private Button retry;
    private TextView status, error, resultsStatus, eyesStatus;
    private LinearLayout resultsCard, eyesCard;
    private final TextView[] clients = new TextView[3], addresses = new TextView[3];
    private String pending="", pendingAction="", issue="";
    private boolean rendering;
    private long lastStatus, submitted;

    private int dp(int n) { return (int)(n*getResources().getDisplayMetrics().density+.5f); }
    private String s(String key) { return localized.getString(getResources().getIdentifier(key,"string",getPackageName())); }
    private TextView text(CharSequence value, int size, int color, boolean bold) {
        TextView v = new TextView(this); v.setText(value); v.setTextSize(size); v.setTextColor(color);
        if (bold) v.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        v.setIncludeFontPadding(false); v.setLineSpacing(dp(4), 1); return v;
    }
    private GradientDrawable shape(int color, int border, int radius) {
        GradientDrawable d = new GradientDrawable(); d.setColor(color); d.setCornerRadius(dp(radius));
        if (border != 0) d.setStroke(dp(1), border); return d;
    }
    private void surface(View v, int color, int border) {
        v.setBackground(new RippleDrawable(ColorStateList.valueOf(0x3079e1c2), shape(color, border, 12), null));
    }
    private Button button(String label, boolean primary) {
        Button b = new Button(this); b.setText(label); b.setAllCaps(false); b.setTextSize(15);
        b.setTextColor(primary ? INK : TEXT); b.setMinHeight(dp(48)); b.setMinimumWidth(0);
        b.setPadding(dp(16), dp(8), dp(16), dp(8)); surface(b, primary ? ACCENT : CARD, primary ? 0 : LINE); return b;
    }
    private LinearLayout column() { LinearLayout view=new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view; }
    private void gap(LinearLayout into,int n) { into.addView(new View(this),new LinearLayout.LayoutParams(1,dp(n))); }
    private LinearLayout card(LinearLayout into) {
        LinearLayout box = column(); box.setPadding(dp(22), dp(18), dp(22), dp(20)); box.setBackground(shape(CARD, LINE, 16));
        into.addView(box, new LinearLayout.LayoutParams(-1, -2)); gap(into, 14); return box;
    }
    private Switch cameraSwitch(String label, String tag) {
        Switch view=new Switch(this); view.setText(s(label)); view.setTextSize(17); view.setTextColor(TEXT); view.setMinHeight(dp(52)); view.setTag(tag);
        int[][] states={new int[]{android.R.attr.state_checked},new int[]{}};
        view.setThumbTintList(new ColorStateList(states,new int[]{ACCENT,0xffa4b0c3}));
        view.setTrackTintList(new ColorStateList(states,new int[]{0xff438e78,0xff4a566b}));
        return view;
    }

    @Override public void onCreate(Bundle bundle) {
        super.onCreate(bundle);
        preferences=getSharedPreferences("panel",MODE_PRIVATE);
        channel=new PanelChannel(new File(createDeviceProtectedStorageContext().getFilesDir(),"control/camera"));
        pending=preferences.getString("camera_pending",""); pendingAction=preferences.getString("camera_action","");
        submitted=SystemClock.elapsedRealtime();
        getWindow().setStatusBarColor(BG); getWindow().setNavigationBarColor(BG);
    }

    private void buildScreen() {
        Configuration config=new Configuration(getResources().getConfiguration());
        String fallback=config.getLocales().get(0).getLanguage().equals("zh")?"zh":"en";
        config.setLocale(Locale.forLanguageTag(preferences.getString("language",fallback))); localized=createConfigurationContext(config);
        LinearLayout root=column(); root.setBackgroundColor(BG);
        LinearLayout header=new LinearLayout(this); header.setGravity(Gravity.CENTER_VERTICAL); header.setPadding(dp(24),dp(14),dp(24),dp(10));
        LinearLayout brand=column(); brand.setPadding(0,0,dp(12),0);
        brand.addView(text("PicoET-Enhance",22,TEXT,true)); gap(brand,4); brand.addView(text(s("home_intro"),13,MUTED,false));
        header.addView(brand,new LinearLayout.LayoutParams(0,-2,1));
        Button language=button(s("language"),false); language.setTag("language");
        language.setOnClickListener(v->{
            String next=localized.getResources().getConfiguration().getLocales().get(0).getLanguage().equals("zh")?"en":"zh";
            preferences.edit().putString("language",next).apply(); buildScreen();
        }); header.addView(language); root.addView(header);
        ScrollView scroll=new ScrollView(this); scroll.setClipToPadding(false); scroll.setPadding(dp(22),dp(12),dp(24),dp(20));
        LinearLayout content=column(); scroll.addView(content); root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));

        LinearLayout transport=card(content);
        toggle=cameraSwitch("camera_title","camera_toggle");
        toggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-on":"camera-off");}); transport.addView(toggle);
        gap(transport,5); status=text("",14,ACCENT,true); status.setTag("camera_state"); transport.addView(status); gap(transport,10);
        transport.addView(text(s("camera_hint"),14,MUTED,false)); gap(transport,12);
        transport.addView(text(s("camera_privacy"),13,0xffffcf80,false)); gap(transport,18);
        for(int i=0;i<3;++i) {
            LinearLayout row=new LinearLayout(this); row.setGravity(Gravity.CENTER_VERTICAL);
            row.addView(text(s("camera_"+CameraState.STREAMS[i]),16,TEXT,true),new LinearLayout.LayoutParams(0,-2,1));
            clients[i]=text("",13,MUTED,false); clients[i].setTag("camera_clients_"+CameraState.STREAMS[i]); row.addView(clients[i]); transport.addView(row);
            addresses[i]=text("",14,TECH,false); addresses[i].setTextIsSelectable(true); addresses[i].setTag("camera_url_"+CameraState.STREAMS[i]);
            transport.addView(addresses[i]); gap(transport,16);
        }
        error=text("",13,0xffffa7a7,false); error.setTextIsSelectable(true); error.setTag("camera_error"); transport.addView(error);
        retry=button("",false); retry.setTag("camera_retry");
        retry.setOnClickListener(v->request(camera.enabled?"camera-on":"camera-off")); transport.addView(retry);

        resultsCard=card(content);
        resultsToggle=cameraSwitch("camera_results_title","camera_results_toggle");
        resultsToggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-results-on":"camera-results-off");});
        resultsCard.addView(resultsToggle);
        resultsStatus=text("",14,ACCENT,true); resultsCard.addView(resultsStatus); gap(resultsCard,10);
        resultsCard.addView(text(s("camera_results_hint"),14,MUTED,false));

        eyesCard=card(content);
        eyesToggle=cameraSwitch("camera_eyes_title","camera_eyes_toggle");
        eyesToggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-eyes-on":"camera-eyes-off");});
        eyesCard.addView(eyesToggle);
        eyesStatus=text("",14,ACCENT,true); eyesCard.addView(eyesStatus); gap(eyesCard,10);
        eyesCard.addView(text(s("camera_eyes_hint"),14,MUTED,false));

        LinearLayout processing=card(content);
        processing.addView(text(s("processing_title"),18,TEXT,true)); gap(processing,10);
        processing.addView(text(s("processing_hint"),14,MUTED,false)); gap(processing,18);
        Button open=button(s("processing_open"),true); open.setTag("open_processing");
        open.setOnClickListener(v->startActivity(new Intent(this,ProcessingActivity.class))); processing.addView(open);
        setContentView(root); render();
    }

    private void render() {
        rendering=true;
        toggle.setChecked(camera!=null && camera.enabled); toggle.setEnabled(camera!=null && pending.isEmpty());
        String phase=pendingAction.equals("camera-on")?"starting":pendingAction.equals("camera-off")?"stopping":camera==null?"loading":camera.state;
        status.setText(s("camera_state_"+phase));
        boolean supportsResults=camera!=null && camera.resultsSupported;
        resultsCard.setVisibility(supportsResults?View.VISIBLE:View.GONE);
        resultsToggle.setChecked(supportsResults && camera.resultsEnabled);
        resultsToggle.setEnabled(supportsResults && pending.isEmpty());
        if(supportsResults) {
            String resultKey=pendingAction.startsWith("camera-results-")?"updating":
                camera.resultsOnline?(camera.resultsEnabled?"on":"pending_off"):camera.resultsEnabled?"pending_on":"off";
            resultsStatus.setText(s("camera_results_"+resultKey).replace("{0}",String.valueOf(camera.resultsClients)));
        }
        boolean supportsEyes=camera!=null && camera.eyesSupported;
        eyesCard.setVisibility(supportsEyes?View.VISIBLE:View.GONE);
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
        int resource=getResources().getIdentifier("camera_error_"+detail,"string",getPackageName());
        error.setText(resource!=0?localized.getString(resource):detail); error.setVisibility(detail.isEmpty()?View.GONE:View.VISIBLE);
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

    private final Runnable poll=new Runnable() {
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
                } else if(SystemClock.elapsedRealtime()-lastStatus>=2000) request("camera-status");
                render();
            } catch(Exception exception) { error.setText(exception.getMessage()); error.setVisibility(View.VISIBLE); }
            handler.postDelayed(this,500);
        }
    };
    @Override protected void onResume() { super.onResume(); buildScreen(); lastStatus=0; handler.post(poll); }
    @Override protected void onPause() { handler.removeCallbacks(poll); super.onPause(); }
}
