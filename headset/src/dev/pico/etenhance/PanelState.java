package dev.pico.etenhance;

/** Only a successful module reply changes saved/applied state. */
final class PanelState {
    ProcessingSettings draft = new ProcessingSettings();
    ProcessingSettings saved, applied;
    boolean savedThisSession;
    boolean ready() { return saved != null; }
    boolean dirty() { return ready() && !draft.same(saved); }
    boolean canSave() { return ready() && dirty(); }
    boolean canApply() { return ready() && !dirty() && !saved.same(applied); }
    String phase() {
        if (!ready()) return "connecting";
        if (dirty()) return "unsaved";
        if (saved.same(applied)) return "applied";
        return savedThisSession ? "saved" : "loaded";
    }
    void loaded(String text) { saved = ProcessingSettings.parse(text); draft = ProcessingSettings.parse(text); savedThisSession = false; }
    void preset(String text) { draft = ProcessingSettings.parse(text); }
    void wrote(String text) { saved = ProcessingSettings.parse(text); draft = ProcessingSettings.parse(text); savedThisSession = true; }
    void applied() { if (saved != null) applied = ProcessingSettings.parse(saved.format()); }
}
