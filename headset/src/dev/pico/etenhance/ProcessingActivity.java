package dev.pico.etenhance;

import android.app.*;
import android.content.*;
import android.content.res.*;
import android.graphics.Typeface;
import android.graphics.drawable.*;
import android.os.*;
import android.text.*;
import android.text.style.*;
import android.view.*;
import android.widget.*;
import java.io.File;
import java.util.*;

/** The tuning panel and shared styling for the companion's single activity. */
abstract class ProcessingActivity extends Activity {
    static final int BG = 0xff10151e, CARD = 0xff1b2533, LINE = 0xff344357, NAV = 0xff141c28;
    static final int TEXT = 0xfff3f6fc, MUTED = 0xffa9b8ce, ACCENT = 0xff79e1c2, INK = 0xff10251e;
    static final int AMBER = 0xffffcf80, TECH = 0xff9fc2ff;
    private static final String[] PRESETS = {"balanced", "responsive", "defaults", "raw"};
    private static final String[] GUIDE = {"read", "time", "flow"};
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final PanelState state = new PanelState();
    private final List<View> formControls = new ArrayList<>();
    private final List<Runnable> live = new ArrayList<>();
    private final Map<String, Button> navigation = new LinkedHashMap<>();
    private PanelChannel channel;
    SharedPreferences preferences;
    private Context localized;
    private LinearLayout panel, host;
    private ScrollView scroll;
    private TextView chip, footerHint, notice;
    private Button saveButton, applyButton, reloadButton;
    private String page = "start", profile = "defaults", pending = "", pendingAction = "";
    private long pendingSubmitted;
    private ProcessingSettings recoveredDraft;
    private boolean rendering, pro, resumed;

    final int id(String key) { return getResources().getIdentifier(key, "string", getPackageName()); }
    final String s(String key) { return localized.getString(id(key)); }
    private String fmt(String key, Object... values) {
        String result = s(key);
        for (int i = 0; i < values.length; ++i) result = result.replace("{" + i + "}", String.valueOf(values[i]));
        return result;
    }
    final int dp(float value) { return (int)(value * getResources().getDisplayMetrics().density + .5f); }
    final int widthDp() { return getResources().getConfiguration().screenWidthDp; }
    final LinearLayout column() { LinearLayout v = new LinearLayout(this); v.setOrientation(LinearLayout.VERTICAL); return v; }
    final LinearLayout row() { LinearLayout v = new LinearLayout(this); v.setGravity(Gravity.CENTER_VERTICAL); return v; }
    final TextView text(CharSequence value, int size, int color, boolean bold) {
        TextView v = new TextView(this); v.setText(value); v.setTextSize(size); v.setTextColor(color);
        if (bold) v.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        v.setIncludeFontPadding(false); v.setLineSpacing(dp(4), 1); return v;
    }
    private GradientDrawable shape(int color, int border, int radius) {
        GradientDrawable d = new GradientDrawable(); d.setColor(color); d.setCornerRadius(dp(radius));
        if (border != 0) d.setStroke(dp(1), border); return d;
    }
    final void surface(View v, int color, int border) {
        v.setBackground(new RippleDrawable(ColorStateList.valueOf(0x3079e1c2), shape(color, border, 12), null));
    }
    final Button button(String label, boolean primary) {
        Button b = new Button(this); b.setText(label); b.setAllCaps(false); b.setTextSize(15);
        b.setTextColor(primary ? INK : TEXT); b.setMinHeight(dp(48)); b.setMinimumWidth(0);
        b.setPadding(dp(16), dp(8), dp(16), dp(8)); surface(b, primary ? ACCENT : CARD, primary ? 0 : LINE); return b;
    }
    final void selected(Button b) { surface(b, 0xff27483f, 0xff4e8e7d); b.setTextColor(ACCENT); }
    final void gap(LinearLayout layout, int height) { View v = new View(this); layout.addView(v, new LinearLayout.LayoutParams(1, dp(height))); }
    final LinearLayout card(LinearLayout into) {
        LinearLayout box = column(); box.setPadding(dp(22), dp(18), dp(22), dp(20)); box.setBackground(shape(CARD, LINE, 16));
        into.addView(box, new LinearLayout.LayoutParams(-1, -2)); gap(into, 14); return box;
    }
    private TextView principle(String body) {
        TextView v = text(body, 13, TECH, false); v.setTextIsSelectable(true); v.setLineSpacing(dp(6), 1);
        v.setPadding(dp(14), dp(12), dp(14), dp(12)); v.setBackground(shape(0xff172234, 0xff2c4566, 10)); return v;
    }
    private <T extends View> T control(T v) { formControls.add(v); return v; }
    private String presetKey(String action) { return action.replace('-', '_'); }
    private void go(String next) { page = next; persist(); buildScreen(); }

    /** Navigation entries in order: start, the movements grouped by face part, all settings, guide. */
    private static List<String> pages() {
        List<String> result = new ArrayList<>(Collections.singletonList("start"));
        for (Catalog.Moment moment : Catalog.MOMENTS) result.add(moment.id);
        result.add("all"); result.add("guide");
        return result;
    }
    private static List<String> fieldsOf(String page) {
        Catalog.Moment moment = Catalog.moment(page);
        if (moment != null) return Arrays.asList(moment.fields);
        List<String> result = new ArrayList<>();
        for (ProcessingSettings.Field field : ProcessingSettings.FIELDS)
            if (page.equals("all") || page.equals("start") && field.toggle) result.add(field.key);
        return result;
    }

    @Override public void onCreate(Bundle savedInstance) {
        super.onCreate(savedInstance);
        getWindow().setStatusBarColor(BG); getWindow().setNavigationBarColor(BG);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        preferences = getSharedPreferences("panel", MODE_PRIVATE);
        Configuration config = new Configuration(getResources().getConfiguration());
        String fallback = config.getLocales().get(0).getLanguage().equals("zh") ? "zh" : "en";
        config.setLocale(Locale.forLanguageTag(preferences.getString("language", fallback)));
        localized = createConfigurationContext(config);
        page = preferences.getString("page", "start"); if (!pages().contains(page)) page = "start";
        profile = preferences.getString("profile", "defaults");
        pro = "pro".equals(preferences.getString("wording", "simple"));
        String saved = preferences.getString("saved_snapshot", ""), draft = preferences.getString("draft", "");
        try {
            if (!saved.isEmpty()) state.loaded(saved);
            if (!draft.isEmpty()) state.preset(draft);
            if (saved.isEmpty() && !state.draft.same(new ProcessingSettings())) recoveredDraft = state.draft;
            if (savedInstance != null && savedInstance.containsKey("applied_snapshot"))
                state.applied = ProcessingSettings.parse(savedInstance.getString("applied_snapshot"));
        } catch (IllegalArgumentException error) { Toast.makeText(this, s("draft_invalid"), Toast.LENGTH_LONG).show(); }
        channel = new PanelChannel(new File(createDeviceProtectedStorageContext().getFilesDir(), "control"));
        pending = preferences.getString("pending", ""); pendingAction = preferences.getString("pending_action", "");
        if (pendingAction.equals("read")) {
            pending = ""; pendingAction = "";
            preferences.edit().remove("pending").remove("pending_action").apply();
        }
        pendingSubmitted = SystemClock.elapsedRealtime();
    }

    final void changeLanguage() {
        String next = localized.getResources().getConfiguration().getLocales().get(0).getLanguage().equals("zh") ? "en" : "zh";
        preferences.edit().putString("language", next).apply();
        recreate();
    }

    final View tuningPanel() {
        if (panel != null) return panel;
        panel = column(); panel.setTag("processing_panel");
        buildScreen();
        if (pending.isEmpty() && !state.dirty()) request("read");
        if (resumed) handler.post(poll);
        return panel;
    }

    private void buildScreen() {
        LinearLayout root = column(); root.setBackgroundColor(BG);
        LinearLayout header = row(); header.setPadding(dp(24), dp(6), dp(24), dp(8));
        header.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
        LinearLayout wording = row(); wording.setPadding(0, 0, dp(12), 0);
        for (boolean option : new boolean[]{false, true}) {
            Button mode = button(s(option ? "wording_pro" : "wording_simple"), false); mode.setTag(option ? "mode_pro" : "mode_simple");
            if (pro == option) selected(mode);
            mode.setContentDescription(s("wording_label") + " " + mode.getText());
            mode.setOnClickListener(v -> setWording(option));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, -2); lp.leftMargin = option ? dp(4) : 0; wording.addView(mode, lp);
        }
        header.addView(wording);
        reloadButton = button(s("reload"), false); reloadButton.setTag("reload");
        reloadButton.setOnClickListener(v -> replaceDraft("read")); header.addView(reloadButton);
        root.addView(header);

        boolean wide = widthDp() >= 760;
        LinearLayout body = new LinearLayout(this); body.setOrientation(wide ? LinearLayout.HORIZONTAL : LinearLayout.VERTICAL);
        LinearLayout list = wide ? column() : row();
        list.setPadding(dp(14), dp(4), dp(10), dp(12));
        navigation.clear();
        String part = "";
        for (String entry : pages()) {
            Catalog.Moment moment = Catalog.moment(entry);
            String group = moment == null ? "" : moment.part();
            if (wide && !group.equals(part)) {
                gap(list, part.isEmpty() ? 10 : 14);
                TextView title = text(group.isEmpty() ? "" : s("part_" + group), 12, MUTED, true);
                title.setPadding(dp(10), 0, 0, dp(6)); if (!group.isEmpty()) list.addView(title);
            }
            if (wide && group.isEmpty() && moment == null && !entry.equals("start") && !part.isEmpty()) { gap(list, 14); }
            part = group;
            Button item = button(s(moment == null ? "nav_" + entry : "m_" + entry), false); item.setTag("nav_" + entry);
            item.setTextSize(14); item.setGravity(Gravity.CENTER_VERTICAL | Gravity.START); item.setMinHeight(dp(42));
            item.setPadding(dp(14), dp(6), dp(10), dp(6));
            if (page.equals(entry)) selected(item); else surface(item, NAV, 0);
            item.setOnClickListener(v -> go(entry));
            LinearLayout.LayoutParams lp = wide ? new LinearLayout.LayoutParams(-1, -2) : new LinearLayout.LayoutParams(-2, dp(44));
            lp.bottomMargin = wide ? dp(4) : 0; lp.rightMargin = wide ? 0 : dp(6); list.addView(item, lp);
            navigation.put(entry, item);
        }
        if (wide) {
            ScrollView side = new ScrollView(this); side.addView(list); side.setBackgroundColor(NAV);
            side.setTag("processing_sidebar");
            body.addView(side, new LinearLayout.LayoutParams(dp(232), -1));
        } else {
            HorizontalScrollView strip = new HorizontalScrollView(this); strip.addView(list);
            body.addView(strip, new LinearLayout.LayoutParams(-1, -2));
        }
        host = column();
        body.addView(host, wide ? new LinearLayout.LayoutParams(0, -1, 1) : new LinearLayout.LayoutParams(-1, 0, 1));
        root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1));

        LinearLayout footer = column(); footer.setPadding(dp(24), dp(12), dp(24), dp(16)); footer.setBackgroundColor(0xff16202d);
        LinearLayout actionRow = row(); LinearLayout explanation = column();
        chip = text("", 14, ACCENT, true); chip.setTag("state"); explanation.addView(chip); gap(explanation, 5);
        footerHint = text("", 12, MUTED, false); footerHint.setMaxLines(3); explanation.addView(footerHint);
        boolean compact = widthDp() < 540;
        if (compact) actionRow.setOrientation(LinearLayout.VERTICAL);
        actionRow.addView(explanation, compact ? new LinearLayout.LayoutParams(-1, -2) : new LinearLayout.LayoutParams(0, -2, 1));
        LinearLayout actionButtons = row();
        LinearLayout.LayoutParams buttonsParams = new LinearLayout.LayoutParams(compact ? -1 : -2, -2);
        buttonsParams.topMargin = compact ? dp(10) : 0; actionRow.addView(actionButtons, buttonsParams);
        saveButton = button(s("save"), false); saveButton.setTag("save"); saveButton.setOnClickListener(v -> request("write"));
        LinearLayout.LayoutParams saveParams = compact ? new LinearLayout.LayoutParams(0, -2, 1) : new LinearLayout.LayoutParams(-2, -2);
        saveParams.leftMargin = compact ? 0 : dp(12); actionButtons.addView(saveButton, saveParams);
        applyButton = button(s("apply"), true); applyButton.setTag("apply");
        applyButton.setOnClickListener(v -> new AlertDialog.Builder(this).setTitle(s("apply"))
                .setMessage(s("apply_warning")).setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(s("apply_confirm"), (dialog, which) -> request("apply")).show());
        LinearLayout.LayoutParams applyParams = compact ? new LinearLayout.LayoutParams(0, -2, 1) : new LinearLayout.LayoutParams(-2, -2);
        applyParams.leftMargin = dp(8); actionButtons.addView(applyButton, applyParams);
        footer.addView(actionRow); notice = text("", 13, MUTED, false); notice.setMaxLines(3); notice.setTag("notice");
        notice.setPadding(0, dp(8), 0, 0); notice.setVisibility(View.GONE); footer.addView(notice); root.addView(footer);
        panel.removeAllViews();
        panel.addView(root, new LinearLayout.LayoutParams(-1, -1)); renderPage(false);
    }

    private void renderPage(boolean keepScroll) {
        int y = keepScroll && scroll != null ? scroll.getScrollY() : 0;
        rendering = true; formControls.clear(); live.clear(); host.removeAllViews();
        Catalog.Moment moment = Catalog.moment(page);
        if (moment != null) momentPage(moment);
        else if (page.equals("all")) allPage();
        else if (page.equals("guide")) guidePage();
        else startPage();
        rendering = false; refreshLive(); refreshControls();
        ScrollView target = scroll; target.post(() -> target.scrollTo(0, y));
    }

    /** A scrolling column that fills the page area. */
    private LinearLayout pageColumn(LinearLayout into, float weight) {
        ScrollView view = new ScrollView(this); view.setClipToPadding(false);
        view.setPadding(dp(22), dp(12), dp(24), dp(20));
        LinearLayout content = column(); view.addView(content);
        into.addView(view, weight == 0 ? new LinearLayout.LayoutParams(-1, -1) : new LinearLayout.LayoutParams(0, -1, weight));
        scroll = view; return content;
    }

    private void heading(LinearLayout into, String title, String description) {
        into.addView(text(title, 24, TEXT, true)); gap(into, 8);
        if (!description.isEmpty()) { into.addView(text(description, 14, MUTED, false)); gap(into, 18); }
    }

    private void startPage() {
        LinearLayout content = pageColumn(host, 0);
        heading(content, s("start_title"), s("start_hint"));
        for (int line = 0; line < 2; ++line) {
            LinearLayout tiles = row();
            for (int col = 0; col < 2; ++col) {
                String action = PRESETS[line * 2 + col], key = presetKey(action);
                LinearLayout tile = control(column()); tile.setTag("preset_" + key); tile.setPadding(dp(18), dp(16), dp(18), dp(16));
                tile.setMinimumHeight(dp(118)); boolean chosen = profile.equals(action);
                surface(tile, chosen ? 0xff203f37 : CARD, chosen ? ACCENT : LINE);
                LinearLayout titleRow = row(); titleRow.addView(text(s("preset_" + key), 18, chosen ? ACCENT : TEXT, true), new LinearLayout.LayoutParams(0, -2, 1));
                if (chosen) titleRow.addView(text("✓", 20, ACCENT, true));
                tile.addView(titleRow); gap(tile, 8);
                tile.addView(text(s("preset_" + key + "_hint"), 13, MUTED, false));
                if (pro) { gap(tile, 8); tile.addView(text(s("preset_" + key + "_pro"), 12, TECH, false)); }
                tile.setContentDescription(s("preset_" + key)); tile.setOnClickListener(v -> replaceDraft(action));
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, -1, 1); lp.rightMargin = col == 0 ? dp(12) : 0; tiles.addView(tile, lp);
            }
            content.addView(tiles); gap(content, 12);
        }
        gap(content, 6);
        LinearLayout switches = card(content);
        masterToggle(switches, ProcessingSettings.field("eye.enabled")); gap(switches, 16);
        masterToggle(switches, ProcessingSettings.field("mouth.enabled"));
        content.addView(text(s("start_steps_title"), 18, TEXT, true)); gap(content, 10);
        LinearLayout steps = card(content);
        TextView body = text(s("start_steps"), 14, TEXT, false); body.setLineSpacing(dp(7), 1); steps.addView(body);
    }

    private void masterToggle(LinearLayout into, ProcessingSettings.Field field) {
        Switch toggle = control(new Switch(this)); toggle.setTag(field.key); toggle.setText(s(field.resource()));
        toggle.setTextColor(TEXT); toggle.setTextSize(17); toggle.setMinHeight(dp(52));
        int[][] states = {new int[]{android.R.attr.state_checked}, new int[]{}};
        toggle.setThumbTintList(new ColorStateList(states, new int[]{ACCENT, 0xffa4b0c3}));
        toggle.setTrackTintList(new ColorStateList(states, new int[]{0xff438e78, 0xff4a566b}));
        into.addView(toggle); gap(into, 5); into.addView(text(s(field.resource() + "_hint"), 13, MUTED, false));
        if (pro) { gap(into, 6); into.addView(text(field.key + " · " + s(field.resource() + "_tech"), 12, TECH, false)); }
        toggle.setOnCheckedChangeListener((button, checked) -> {
            if (rendering) return;
            state.draft.set(field.key, checked ? 1 : 0); edited();
        });
        live.add(() -> toggle.setChecked(state.draft.get(field.key) != 0));
    }

    private SpannableStringBuilder legend(Scope.Scenario scenario, boolean savedShown) {
        SpannableStringBuilder value = new SpannableStringBuilder();
        String[][] items = {{"━━", "legend_model", "in"}, {"━━", "legend_avatar", "out"}, {"╍╍", "legend_saved", "saved"}, {"┈┈", "ctx_" + scenario.id, "ctx"}};
        int[] colors = {ScopeView.INPUT, ScopeView.DRAFT, ScopeView.SAVED, ScopeView.CONTEXT};
        for (int i = 0; i < items.length; ++i) {
            if (i == 2 && !savedShown || i == 3 && scenario.context < 0) continue;
            if (value.length() > 0) value.append("     ");
            int start = value.length();
            value.append(items[i][0]);
            value.setSpan(new ForegroundColorSpan(colors[i]), start, value.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            value.setSpan(new StyleSpan(Typeface.BOLD), start, value.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            value.append(' ').append(s(items[i][1]));
        }
        return value;
    }

    private String reading(String metric, double value) {
        if (Scope.timed(metric)) {
            if (Double.isInfinite(value)) return s("r_not_reached");
            return value < 1000 ? Math.round(value) + " ms" : String.format(Locale.ROOT, "%.2f s", value / 1000);
        }
        double percent = value * 100;
        return (percent > 0 && percent < 10 ? String.format(Locale.ROOT, "%.1f", percent) : String.valueOf(Math.round(percent))) + "%";
    }

    private CharSequence readingLine(Scope.Scenario scenario, String metric, Scope.Trace draft, Scope.Trace saved) {
        SpannableStringBuilder line = new SpannableStringBuilder(s("r_" + scenario.id + "_" + metric) + "  ");
        String now = reading(metric, Scope.metric(metric, scenario, draft.input, draft.output));
        int start = line.length(); line.append(now);
        line.setSpan(new ForegroundColorSpan(ACCENT), start, line.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        line.setSpan(new StyleSpan(Typeface.BOLD), start, line.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        start = line.length();
        if (!metric.equals("lag")) line.append("   ").append(fmt("r_model", reading(metric, Scope.metric(metric, scenario, draft.input, draft.input))));
        line.setSpan(new ForegroundColorSpan(MUTED), start, line.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        if (saved != null) {
            String before = reading(metric, Scope.metric(metric, scenario, saved.input, saved.output));
            if (!before.equals(now)) {
                start = line.length(); line.append("   ").append(fmt("r_saved", before));
                line.setSpan(new ForegroundColorSpan(AMBER), start, line.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
        }
        return line;
    }

    private void momentPage(Catalog.Moment moment) {
        Scope.Scenario scenario = moment.scenario();
        boolean split = widthDp() >= 980;
        LinearLayout left, right;
        if (split) {
            LinearLayout halves = row(); halves.setGravity(Gravity.TOP); host.addView(halves, new LinearLayout.LayoutParams(-1, -1));
            left = pageColumn(halves, 1.05f); right = pageColumn(halves, 1);
        } else { left = pageColumn(host, 0); right = left; }

        left.addView(text(s("part_" + moment.part()), 13, MUTED, true)); gap(left, 6);
        left.addView(text(s("m_" + moment.id), 24, TEXT, true)); gap(left, 10);
        left.addView(text(s("m_" + moment.id + "_look"), 14, TEXT, false)); gap(left, 14);
        TextView off = text(s(moment.master().equals("eye.enabled") ? "off_eye" : "off_mouth"), 13, AMBER, false);
        off.setPadding(0, 0, 0, dp(10)); left.addView(off);
        ScopeView scope = new ScopeView(this, scenario, this::s);
        left.addView(scope, new LinearLayout.LayoutParams(-1, -2)); gap(left, 10);
        TextView key = text("", 12, MUTED, false); left.addView(key); gap(left, 14);
        LinearLayout readings = column(); left.addView(readings);
        List<TextView> lines = new ArrayList<>();
        for (int i = 0; i < scenario.metrics.length; ++i) {
            TextView reading = text("", 15, TEXT, false); reading.setTag("reading_" + scenario.id + "_" + scenario.metrics[i]);
            readings.addView(reading); gap(readings, 8); lines.add(reading);
        }
        gap(left, 6);
        left.addView(text(s("scope_example"), 12, MUTED, false));
        if (pro) { gap(left, 14); left.addView(principle(s("m_" + moment.id + "_pro"))); }
        if (split) gap(left, 20); else gap(left, 26);

        right.addView(text(s("settings_for"), 18, TEXT, true)); gap(right, 4);
        right.addView(text(s("settings_for_hint"), 13, MUTED, false));
        LinearLayout box = card(right); box.setPadding(dp(22), dp(4), dp(22), dp(18));
        for (String field : moment.fields) fieldControl(box, ProcessingSettings.field(field), moment.id);

        live.add(() -> {
            Scope.Trace draft = Scope.run(scenario, state.draft), saved = state.saved == null ? null : Scope.run(scenario, state.saved);
            scope.show(draft, saved);
            boolean differs = saved != null && !Arrays.equals(saved.output, draft.output);
            key.setText(legend(scenario, differs));
            for (int i = 0; i < lines.size(); ++i) lines.get(i).setText(readingLine(scenario, scenario.metrics[i], draft, differs ? saved : null));
            off.setVisibility(state.draft.get(moment.master()) == 0 ? View.VISIBLE : View.GONE);
            StringBuilder description = new StringBuilder(s("m_" + moment.id));
            for (TextView line : lines) description.append(". ").append(line.getText());
            scope.setContentDescription(description);
        });
    }

    private Button fineButton(ProcessingSettings.Field field, int direction) {
        Button arrow = control(button(direction < 0 ? "◀" : "▶", false));
        arrow.setTag(field.key + (direction < 0 ? ".decrease" : ".increase"));
        arrow.setTextSize(16); arrow.setPadding(0, 0, 0, 0);
        arrow.setContentDescription(fmt(direction < 0 ? "fine_decrease" : "fine_increase", s(field.resource()), field.display(field.step)));
        arrow.setOnClickListener(v -> {
            double current = state.draft.get(field.key), next = field.nudge(current, direction);
            if (next == current) return;
            try { state.draft.set(field.key, next); edited(); }
            catch (IllegalArgumentException error) { message(s("blink_order"), true); }
        });
        return arrow;
    }

    private void fieldControl(LinearLayout box, ProcessingSettings.Field field, String moment) {
        gap(box, 16);
        LinearLayout names = row(); TextView name = text(s(field.resource()), 15, TEXT, false);
        names.addView(name, new LinearLayout.LayoutParams(0, -2, 1));
        Button value = control(button("", false)); value.setTag(field.key + ".value"); value.setTextSize(14);
        value.setOnClickListener(v -> exactValue(field)); names.addView(value); box.addView(names);
        LinearLayout adjustments = row();
        adjustments.addView(fineButton(field, -1), new LinearLayout.LayoutParams(dp(48), dp(48)));
        SeekBar slider = control(new SeekBar(this)); slider.setTag(field.key); slider.setMax(1000);
        slider.setProgressTintList(ColorStateList.valueOf(ACCENT)); slider.setThumbTintList(ColorStateList.valueOf(ACCENT));
        slider.setContentDescription(s(field.resource()));
        LinearLayout.LayoutParams sliderParams = new LinearLayout.LayoutParams(0, dp(48), 1);
        sliderParams.leftMargin = dp(6); sliderParams.rightMargin = dp(6);
        adjustments.addView(slider, sliderParams);
        adjustments.addView(fineButton(field, 1), new LinearLayout.LayoutParams(dp(48), dp(48)));
        box.addView(adjustments, new LinearLayout.LayoutParams(-1, -2));
        LinearLayout ends = row(); ends.setGravity(Gravity.TOP);
        TextView less = text("← " + s(field.resource() + "_less"), 12, MUTED, false);
        TextView more = text(s(field.resource() + "_more") + " →", 12, MUTED, false); more.setGravity(Gravity.END);
        ends.addView(less, new LinearLayout.LayoutParams(0, -2, 1));
        LinearLayout.LayoutParams moreParams = new LinearLayout.LayoutParams(0, -2, 1); moreParams.leftMargin = dp(16);
        ends.addView(more, moreParams); box.addView(ends);
        List<String> others = new ArrayList<>();
        for (Catalog.Moment other : Catalog.MOMENTS)
            if (!other.id.equals(moment) && Arrays.asList(other.fields).contains(field.key)) others.add(s("m_" + other.id));
        if (!others.isEmpty()) {
            TextView shared = text(fmt("also_moves", String.join(s("list_separator"), others)), 12, MUTED, false);
            shared.setPadding(0, dp(6), 0, 0); box.addView(shared);
        }
        if (pro) {
            TextView tech = text(field.key + " · " + s(field.resource() + "_tech"), 12, TECH, false);
            tech.setPadding(0, dp(6), 0, 0); box.addView(tech);
        }
        TextView note = text("", 12, AMBER, false); note.setPadding(0, dp(6), 0, 0); box.addView(note);
        final boolean[] tracking = {false};
        slider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar bar, int progress, boolean fromUser) {
                if (!fromUser || rendering) return;
                try { state.draft.set(field.key, field.value(progress)); edited(); }
                catch (IllegalArgumentException error) { message(s("blink_order"), true); }
            }
            @Override public void onStartTrackingTouch(SeekBar bar) { tracking[0] = true; }
            @Override public void onStopTrackingTouch(SeekBar bar) { tracking[0] = false; refreshLive(); }
        });
        live.add(() -> {
            double current = state.draft.get(field.key);
            value.setText(field.brief(current));
            value.setTextColor(state.draft.differs(field.key, state.saved) ? AMBER : ACCENT);
            value.setContentDescription(s(field.resource()) + ", " + value.getText());
            if (!tracking[0]) slider.setProgress(field.progress(current));
            String[] why = Catalog.inactive(state.draft, field);
            boolean shown = why != null && !why[0].equals("inactive_region");
            note.setText(shown ? fmt(why[0], s(ProcessingSettings.field(why[1]).resource())) : "");
            note.setVisibility(shown ? View.VISIBLE : View.GONE);
            float alpha = why == null ? 1 : .5f; name.setAlpha(alpha); adjustments.setAlpha(alpha); ends.setAlpha(alpha);
        });
    }

    private void allPage() {
        LinearLayout content = pageColumn(host, 0);
        heading(content, s("nav_all"), s("all_hint"));
        for (String group : Catalog.STEPS) {
            LinearLayout box = card(content); box.setTag("step_" + group);
            box.addView(text(s("step_" + group), 17, TEXT, true)); gap(box, 6);
            box.addView(text(s("step_" + group + "_hint"), 13, MUTED, false));
            List<ProcessingSettings.Field> fields = Catalog.step(group);
            if (fields.get(0).toggle) { gap(box, 10); masterToggle(box, fields.get(0)); continue; }
            for (ProcessingSettings.Field field : fields) fieldControl(box, field, null);
            Set<String> seen = new LinkedHashSet<>();
            for (Catalog.Moment moment : Catalog.MOMENTS)
                for (ProcessingSettings.Field field : fields) if (Arrays.asList(moment.fields).contains(field.key)) seen.add(moment.id);
            gap(box, 12);
            LinearLayout links = row(); links.addView(text(s("see_effect"), 12, MUTED, false));
            for (String moment : seen) {
                Button link = button(s("m_" + moment), false); link.setTextSize(12); link.setMinHeight(dp(36));
                link.setPadding(dp(10), dp(4), dp(10), dp(4)); link.setTag("link_" + group + "_" + moment);
                link.setOnClickListener(v -> go(moment));
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, -2); lp.leftMargin = dp(8); links.addView(link, lp);
            }
            HorizontalScrollView strip = new HorizontalScrollView(this); strip.addView(links); box.addView(strip);
        }
    }

    private void guidePage() {
        LinearLayout content = pageColumn(host, 0);
        heading(content, s("nav_guide"), "");
        for (String name : GUIDE) {
            LinearLayout box = card(content);
            box.addView(text(s("guide_" + name + "_title"), 17, TEXT, true)); gap(box, 10);
            TextView body = text(s("guide_" + name), 14, TEXT, false); body.setTag("guide_" + name);
            body.setLineSpacing(dp(6), 1); body.setTextIsSelectable(true); box.addView(body);
            if (pro && name.equals("time")) { gap(box, 12); box.addView(principle(s("guide_" + name + "_pro"))); }
        }
    }

    private AlertDialog exactValue(ProcessingSettings.Field field) {
        LinearLayout content = column(); content.setPadding(dp(24), dp(12), dp(24), 0);
        EditText number = new EditText(this); number.setSingleLine(true); number.setTag("exact_value"); number.setTextColor(TEXT);
        number.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
        String shown = field.percent ? field.display(state.draft.get(field.key)).replace("%", "") : ProcessingSettings.number(state.draft.get(field.key));
        number.setText(shown); number.selectAll(); content.addView(number);
        content.addView(text(fmt(field.percent ? "range_percent" : "range", field.display(field.min), field.display(field.max), field.brief(field.initial)), 12, MUTED, false));
        gap(content, 14);
        content.addView(text(s("direction_less") + s(field.resource() + "_less"), 14, TEXT, false)); gap(content, 6);
        content.addView(text(s("direction_more") + s(field.resource() + "_more"), 14, TEXT, false));
        if (pro) { gap(content, 12); content.addView(text(field.key + " · " + s(field.resource() + "_tech"), 13, TECH, false)); }
        gap(content, 16);
        ScrollView scroller = new ScrollView(this); scroller.addView(content);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(s(field.resource())).setView(scroller)
                .setNegativeButton(android.R.string.cancel, null).setNeutralButton(s("reset_value"), null)
                .setPositiveButton(s("done"), null).create();
        dialog.setOnShowListener(ignored -> {
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(v -> {
                try { state.draft.set(field.key, field.initial); edited(); dialog.dismiss(); }
                catch (IllegalArgumentException error) { number.setError(s("blink_order")); }
            });
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
                try {
                    String input = number.getText().toString().trim().replace(',', '.');
                    if (input.equals(shown)) { dialog.dismiss(); return; }
                    state.draft.set(field.key, Double.parseDouble(input) / (field.percent ? 100 : 1));
                    edited(); dialog.dismiss();
                } catch (IllegalArgumentException error) { number.setError(s("blink_order".equals(error.getMessage()) ? "blink_order" : "invalid_number")); }
            });
        }); dialog.show(); return dialog;
    }

    /** Changes only the wording; the draft, presets, saved and applied settings stay as they are. */
    private void setWording(boolean value) {
        if (pro == value) return;
        pro = value;
        preferences.edit().putString("wording", value ? "pro" : "simple").apply();
        buildScreen();
    }

    private void edited() {
        profile = state.draft.same(new ProcessingSettings()) ? "defaults" : "custom";
        persist(); refreshLive(); refreshControls(); message("", false);
    }
    private void replaceDraft(String action) {
        if (!state.dirty()) { request(action); return; }
        new AlertDialog.Builder(this).setTitle(s("discard_title")).setMessage(s("discard_hint"))
                .setNegativeButton(android.R.string.cancel, null).setPositiveButton(s("replace"), (dialog, which) -> request(action)).show();
    }
    private void request(String action) {
        if (!pending.isEmpty()) return;
        try {
            pending = channel.submit(action, state.draft.format()); pendingAction = action;
            pendingSubmitted = SystemClock.elapsedRealtime();
            if (!action.equals("read"))
                preferences.edit().putString("pending", pending).putString("pending_action", action).apply();
            message("", false); refreshControls();
        } catch (Exception error) { message(s("failed") + " " + error.getMessage(), true); }
    }
    private void refreshLive() {
        boolean previous = rendering; rendering = true;
        for (Runnable update : live) update.run();
        for (Map.Entry<String, Button> item : navigation.entrySet()) {
            int changed = 0;
            for (String key : fieldsOf(item.getKey())) if (state.draft.differs(key, state.saved)) ++changed;
            Catalog.Moment moment = Catalog.moment(item.getKey());
            String label = s(moment == null ? "nav_" + item.getKey() : "m_" + item.getKey());
            SpannableStringBuilder value = new SpannableStringBuilder(label);
            if (changed > 0) {
                value.append("  ●").append(String.valueOf(changed));
                value.setSpan(new ForegroundColorSpan(AMBER), label.length(), value.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            }
            item.getValue().setText(value);
            item.getValue().setContentDescription(changed > 0 ? label + ", " + fmt("changed_badge", changed) : label);
        }
        rendering = previous;
    }
    private void refreshControls() {
        boolean busy = !pending.isEmpty();
        for (View v : formControls) v.setEnabled(!busy && state.ready());
        host.setAlpha(busy || !state.ready() ? .6f : 1);
        reloadButton.setEnabled(!busy); saveButton.setEnabled(!busy && state.canSave()); applyButton.setEnabled(!busy && state.canApply());
        saveButton.setAlpha(saveButton.isEnabled() ? 1 : .4f); applyButton.setAlpha(applyButton.isEnabled() ? 1 : .4f);
        chip.setText(busy ? s("state_waiting") : state.dirty() ? fmt("state_unsaved_count", state.draft.differences(state.saved)) : s("state_" + state.phase()));
        chip.setTextColor(state.dirty() ? AMBER : ACCENT);
        footerHint.setText(s(busy ? "waiting" : "hint_" + state.phase()));
    }
    private void message(String text, boolean error) {
        notice.setText(text); notice.setTextColor(error ? 0xffffa7a7 : MUTED); notice.setVisibility(text.isEmpty() ? View.GONE : View.VISIBLE);
    }
    private final Runnable poll = new Runnable() {
        @Override public void run() {
            try {
                if (!pending.isEmpty()) {
                    String[] response = channel.response(pending);
                    if (response != null) {
                        String action = pendingAction; pending = ""; pendingAction = "";
                        preferences.edit().remove("pending").remove("pending_action").apply();
                        if (!response[0].equals("0")) { refreshControls(); message(s("failed") + " " + response[1], true); }
                        else {
                            if (action.equals("read")) {
                                state.loaded(response[1]);
                                if (recoveredDraft != null) { state.draft = recoveredDraft; recoveredDraft = null; }
                                profile = state.draft.same(new ProcessingSettings()) ? "defaults" : "custom";
                            }
                            else if (action.equals("write")) state.wrote(response[1]);
                            else if (action.equals("apply")) state.applied();
                            else if (action.equals("check")) message(s("valid"), false);
                            else { state.preset(response[1]); profile = action; }
                            persist(); renderPage(true);
                        }
                    } else if (SystemClock.elapsedRealtime() - pendingSubmitted > 15000 &&
                               (pendingAction.equals("read") || !channel.requestQueued())) {
                        pending = ""; pendingAction = "";
                        preferences.edit().remove("pending").remove("pending_action").apply();
                        refreshControls();
                        message(s("failed") + " worker_unavailable", true);
                    }
                }
            } catch (Exception error) { refreshControls(); message(s("failed") + " " + error.getMessage(), true); }
            handler.postDelayed(this, 500);
        }
    };
    private void persist() {
        SharedPreferences.Editor editor = preferences.edit().putString("draft", state.draft.format())
                .putString("page", page).putString("profile", profile).remove("symptom");
        if (state.saved != null) editor.putString("saved_snapshot", state.saved.format()); editor.apply();
    }
    @Override protected void onSaveInstanceState(Bundle bundle) {
        if (state.applied != null) bundle.putString("applied_snapshot", state.applied.format()); super.onSaveInstanceState(bundle);
    }
    @Override protected void onResume() {
        super.onResume(); resumed = true;
        if (panel != null) handler.post(poll);
    }
    @Override protected void onPause() {
        resumed = false; handler.removeCallbacks(poll);
        if (panel != null) persist();
        super.onPause();
    }
}
