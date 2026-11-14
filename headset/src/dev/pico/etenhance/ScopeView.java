package dev.pico.etenhance;

import android.content.Context;
import android.graphics.*;
import android.view.View;

/**
 * One movement over time: the model's value in grey, what the avatar receives in colour
 * (the draft solid, the saved settings dashed where they differ), and a related raw signal faintly.
 */
final class ScopeView extends View {
    interface Text { String label(String key); }

    static final int DRAFT = 0xff79e1c2, SAVED = 0xffd7e3f5, INPUT = 0xff6f8098, CONTEXT = 0xff8fb8ff, MARK = 0xffffcf80;
    private static final int GRID = 0xff26354a, AXIS = 0xff52637a, LABEL = 0xffa9b8ce, PANEL = 0xff131b27;
    private static final float HEIGHT = 230, TOP = 16, LEFT = 46, RIGHT = 14, BOTTOM = 30;

    private final Scope.Scenario scenario;
    private final Text text;
    private final float dp, sp;
    private final Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG), fill = new Paint(Paint.ANTI_ALIAS_FLAG), words = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final DashPathEffect dash, dots;
    private final RectF area = new RectF();
    private Scope.Trace draft, saved;
    private double top = 1;

    ScopeView(Context context, Scope.Scenario scenario, Text text) {
        super(context);
        this.scenario = scenario; this.text = text;
        dp = context.getResources().getDisplayMetrics().density;
        sp = context.getResources().getDisplayMetrics().scaledDensity;
        dash = new DashPathEffect(new float[]{6 * dp, 4 * dp}, 0);
        dots = new DashPathEffect(new float[]{2 * dp, 4 * dp}, 0);
        stroke.setStyle(Paint.Style.STROKE); stroke.setStrokeJoin(Paint.Join.ROUND); stroke.setStrokeCap(Paint.Cap.ROUND);
        setTag("scope_" + scenario.id);
    }

    void show(Scope.Trace draft, Scope.Trace saved) {
        this.draft = draft;
        this.saved = saved != null && !java.util.Arrays.equals(saved.output, draft.output) ? saved : null;
        top = Scope.ceiling(Scope.max(draft.input), Scope.max(draft.output), Scope.max(draft.context) * .6,
                this.saved == null ? 0 : Scope.max(this.saved.output));
        invalidate();
    }

    @Override protected void onMeasure(int widthSpec, int heightSpec) {
        setMeasuredDimension(MeasureSpec.getSize(widthSpec), (int)(HEIGHT * dp));
    }

    private float x(int frame) { return area.left + area.width() * frame / (draft.output.length - 1f); }
    private float y(double value) { return area.bottom - (float)(Math.min(value, top * 1.02) / top) * area.height(); }

    private void label(Canvas canvas, String value, float left, float baseline, float size, int color, Paint.Align align) {
        words.setTextSize(size * sp); words.setColor(color); words.setTextAlign(align);
        canvas.drawText(value, left, baseline, words);
    }

    private void line(Canvas canvas, double[] values, int color, float width, PathEffect effect, double scale) {
        Path path = new Path();
        for (int n = 0; n < values.length; ++n) {
            float px = x(n), py = y(values[n] * scale);
            if (n == 0) path.moveTo(px, py); else path.lineTo(px, py);
        }
        stroke.setColor(color); stroke.setStrokeWidth(width * dp); stroke.setPathEffect(effect);
        canvas.drawPath(path, stroke);
    }

    @Override protected void onDraw(Canvas canvas) {
        if (draft == null) return;
        fill.setColor(PANEL);
        canvas.drawRoundRect(0, 0, getWidth(), getHeight(), 10 * dp, 10 * dp, fill);
        area.set(LEFT * dp, TOP * dp, getWidth() - RIGHT * dp, (HEIGHT - BOTTOM) * dp);
        stroke.setPathEffect(null); stroke.setStrokeWidth(dp);
        for (int i = 0; i <= 4; ++i) {
            double value = top * i / 4;
            stroke.setColor(i == 0 ? AXIS : GRID);
            canvas.drawLine(area.left, y(value), area.right, y(value), stroke);
            label(canvas, Math.round(value * 100) + "%", area.left - 6 * dp, y(value) + 4 * dp, 11, LABEL, Paint.Align.RIGHT);
        }
        int seconds = (int)Math.round(scenario.seconds * 2);
        for (int i = 0; i <= seconds; ++i) {
            int frame = Scope.index(i / 2.0);
            stroke.setColor(i == 0 ? AXIS : GRID); stroke.setPathEffect(null); stroke.setStrokeWidth(dp);
            canvas.drawLine(x(frame), area.top, x(frame), area.bottom, stroke);
            String tick = i % 2 == 0 ? (i / 2) + " s" : "";
            label(canvas, tick, x(frame), area.bottom + 18 * dp, 11, LABEL, i == 0 ? Paint.Align.LEFT : i == seconds ? Paint.Align.RIGHT : Paint.Align.CENTER);
        }
        canvas.save();
        canvas.clipRect(area.left, area.top - 2 * dp, area.right, area.bottom + 2 * dp);
        if (scenario.context >= 0) line(canvas, draft.context, CONTEXT, 1.4f, dots, 1);
        line(canvas, draft.input, INPUT, 2f, null, 1);
        if (saved != null) line(canvas, saved.output, SAVED, 1.8f, dash, 1);
        line(canvas, draft.output, DRAFT, 3f, null, 1);
        canvas.restore();
        for (String metric : scenario.metrics) {
            int at = Scope.frame(metric, scenario, draft.input, draft.output);
            if (at < 0 || Scope.timed(metric) && metric.equals("lag")) continue;
            fill.setColor(MARK);
            canvas.drawCircle(x(at), y(draft.output[at]), 4.5f * dp, fill);
        }
    }
}
