package dev.pico.etenhance;

import android.app.Activity;
import android.content.*;
import android.content.res.*;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.io.File;
import java.util.Locale;

/** Companion home: independent camera transport and the existing tuning panel. */
public final class MainActivity extends Activity {
    private static final int BG=0xff10151e, CARD=0xff1b2533, TEXT=0xfff3f6fc, MUTED=0xffa9b8ce, ACCENT=0xff79e1c2;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private SharedPreferences preferences;
    private Context localized;
    private PanelChannel channel;
    private CameraState camera;
    private Switch toggle;
    private Button retry;
    private TextView status, error;
    private final TextView[] clients = new TextView[3], addresses = new TextView[3];
    private String pending="", pendingAction="", issue="";
    private boolean rendering;
    private long lastStatus, submitted;

    private int dp(int n) { return (int)(n*getResources().getDisplayMetrics().density+.5f); }
    private String s(String key) { return localized.getString(getResources().getIdentifier(key,"string",getPackageName())); }
    private TextView text(String value, int size, int color, boolean bold) {
        TextView view=new TextView(this); view.setText(value); view.setTextSize(size); view.setTextColor(color);
        if(bold)view.setTypeface(Typeface.DEFAULT,Typeface.BOLD);
        view.setLineSpacing(dp(4),1); return view;
    }
    private LinearLayout column() { LinearLayout view=new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view; }
    private void gap(LinearLayout into,int n) { into.addView(new View(this),new LinearLayout.LayoutParams(1,dp(n))); }
    private LinearLayout section(LinearLayout into) {
        LinearLayout box=column(); box.setPadding(dp(24),dp(22),dp(24),dp(22));
        GradientDrawable background=new GradientDrawable(); background.setColor(CARD); background.setCornerRadius(dp(16)); box.setBackground(background);
        into.addView(box,new LinearLayout.LayoutParams(-1,-2)); gap(into,18); return box;
    }
    private Button button(String value) { Button view=new Button(this); view.setText(value); view.setAllCaps(false); view.setMinHeight(dp(52)); return view; }

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
        LinearLayout root=column(); root.setBackgroundColor(BG); root.setPadding(dp(28),dp(18),dp(28),dp(18));
        LinearLayout header=new LinearLayout(this); header.setGravity(Gravity.CENTER_VERTICAL);
        header.addView(text("PicoET-Enhance",26,TEXT,true),new LinearLayout.LayoutParams(0,-2,1));
        Button language=button(s("language")); language.setTag("language");
        language.setOnClickListener(v->{
            String next=localized.getResources().getConfiguration().getLocales().get(0).getLanguage().equals("zh")?"en":"zh";
            preferences.edit().putString("language",next).apply(); buildScreen();
        }); header.addView(language); root.addView(header); gap(root,8); root.addView(text(s("home_intro"),14,MUTED,false)); gap(root,20);
        ScrollView scroll=new ScrollView(this); LinearLayout content=column(); scroll.addView(content); root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));

        LinearLayout transport=section(content);
        toggle=new Switch(this); toggle.setText(s("camera_title")); toggle.setTextSize(22); toggle.setTextColor(TEXT); toggle.setMinHeight(dp(60)); toggle.setTag("camera_toggle");
        int[][] states={new int[]{android.R.attr.state_checked},new int[]{}};
        toggle.setThumbTintList(new ColorStateList(states,new int[]{ACCENT,0xffa4b0c3}));
        toggle.setTrackTintList(new ColorStateList(states,new int[]{0xff438e78,0xff4a566b}));
        toggle.setOnCheckedChangeListener((view,checked)->{if(!rendering)request(checked?"camera-on":"camera-off");}); transport.addView(toggle);
        status=text("",16,ACCENT,true); status.setTag("camera_state"); transport.addView(status); gap(transport,10);
        transport.addView(text(s("camera_hint"),14,MUTED,false)); gap(transport,12);
        transport.addView(text(s("camera_privacy"),13,0xffffcf80,false)); gap(transport,18);
        for(int i=0;i<3;++i) {
            LinearLayout row=new LinearLayout(this); row.setGravity(Gravity.CENTER_VERTICAL);
            row.addView(text(s("camera_"+CameraState.STREAMS[i]),16,TEXT,true),new LinearLayout.LayoutParams(0,-2,1));
            clients[i]=text("",13,MUTED,false); clients[i].setTag("camera_clients_"+CameraState.STREAMS[i]); row.addView(clients[i]); transport.addView(row);
            addresses[i]=text("",14,MUTED,false); addresses[i].setTextIsSelectable(true); addresses[i].setTag("camera_url_"+CameraState.STREAMS[i]);
            transport.addView(addresses[i]); gap(transport,16);
        }
        error=text("",13,0xffffa7a7,false); error.setTextIsSelectable(true); error.setTag("camera_error"); transport.addView(error);
        retry=button(""); retry.setTag("camera_retry");
        retry.setOnClickListener(v->request(camera.enabled?"camera-on":"camera-off")); transport.addView(retry);

        LinearLayout processing=section(content);
        processing.addView(text(s("processing_title"),22,TEXT,true)); gap(processing,10);
        processing.addView(text(s("processing_hint"),14,MUTED,false)); gap(processing,18);
        Button open=button(s("processing_open")); open.setTag("open_processing");
        open.setOnClickListener(v->startActivity(new Intent(this,ProcessingActivity.class))); processing.addView(open);
        setContentView(root); render();
    }

    private void render() {
        rendering=true;
        toggle.setChecked(camera!=null && camera.enabled); toggle.setEnabled(camera!=null && pending.isEmpty());
        String phase=!pending.isEmpty() && !pendingAction.equals("camera-status") ? pendingAction.equals("camera-on")?"starting":"stopping" : camera==null?"loading":camera.state;
        status.setText(s("camera_state_"+phase));
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
