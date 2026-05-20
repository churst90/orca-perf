Subject: New feature for Orca: NVDA-style OCR buffer (closes #706, #249, #670, #202)

Hi Joanie,

I've built a content-recognition feature for Orca that closes four
long-standing open issues at once -- most notably #706 (the recent
"select and copy from flat review" request, which has terminal text
selection as its motivating use case). The implementation is
opt-in, single-keybinding, and uses entirely stable internal API
that already exists on upstream/main.

The short version: Orca+R captures the focused window's pixels,
runs Tesseract over them, and lets the user navigate the recognized
text with NumPad keys -- character, word, and line granularity --
plus Shift+nav for selection and NumPad / for click-through. There
is no GTK widget anywhere; the buffer lives purely in the
presenter's state and the keyboard intercept goes through Orca's
existing Atspi.Device listener via command_manager. Synthesized
clicks land on the source window because no Orca surface overlaps
it.

Attached:

1. ISSUE_BODY.md
   The full issue text. I'd like to file this as a feature
   request on gitlab.gnome.org/GNOME/orca and reference #706,
   #249, #670, #202. Posting as-is unless you'd rather I split
   it differently.

2. 0001-ocr_presenter-Add-NVDA-style-OCR-buffer-with-click-p.patch
   Single squashed commit against upstream/main HEAD b20d990c6.
   58 KB, 6 files changed, 1457 insertions, 0 deletions. Applies
   cleanly (git apply --check passes). 4 new modules in
   src/orca/ (ocr_buffer.py, ocr_capture.py, ocr_engine.py,
   ocr_presenter.py), one meson.build line each, one entry added
   to default.Script._register_builtin_extensions.

3. PRODUCTION_READINESS.md
   Honest assessment of what works today vs. what still needs
   polish before this is upstream-merge-ready (i18n strings,
   unit tests, Wayland portal capture, settings schema, user
   docs). All of these are additive and I'm happy to do them as
   follow-up patches once you've had a chance to weigh in on the
   architecture.

4. files-to-attach/
   Loose copies of the four .py modules if you prefer reading
   them outside the patch context.

Two things I want to flag up front since they're either design
calls or borderline-private-API:

- I use `command_manager._keyboard_commands.values()` once during
  mode entry to find external commands whose bindings need to be
  suspended. Happy to add a public iter helper if you'd prefer
  the surface stay clean.

- The Extension class registers OCR's commands as a separate
  group ("OCR" for now, would become guilabels.KB_GROUP_OCR). I
  followed the same registration pattern flat_review_presenter
  and notification_presenter use, including the suspend/restore
  pattern for keys that overlap flat-review.

A note on process: I followed the discipline laid out in
UPSTREAM_SUBMISSION_PROCESS.md after the #711 and #712 lessons --
this was developed on a branch forked from upstream/main, every
referenced function was read from upstream not the perf branch,
the patch applies cleanly via git apply --check, and the issue
body cites upstream code paths.

The feature itself was iterated heavily on my perf branch (you
can see the commit progression at
github.com/churst90/orca-perf -- look for the ocr_* commits)
before settling on the design that's in this single patch. The
final architecture survived testing against the prefs window,
the desktop, terminals, and inaccessible apps; the user reports
clicking on icons works as expected.

Let me know if you'd like me to file the GitLab issue or hold off
pending your review. Either way -- and either outcome -- thanks
for the consistent and detailed feedback over the past few weeks.
It made this end up much better than where I started.

Cody Hurst
codythurst@gmail.com
