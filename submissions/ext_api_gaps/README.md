# User-extension API gaps — found while converting OCR

Each `.md` file in this directory is a complete GitLab issue body
ready to paste at <https://gitlab.gnome.org/GNOME/orca/-/issues/new>.

## Recommended filing order

| File | Why this order | Joanie's likely path |
|---|---|---|
| `01-loader-sys-modules-bug.md` | **File first** — actual bug, blocks any extension using @dataclass on Python 3.14, one-line fix obvious | Likely "take a stab at it" |
| `02-controller-active-window.md` | Smallest gimme API, low blast radius | Likely "take a stab" |
| `03-controller-clipboard.md` | Same: small wrapper, obvious shape | Likely "take a stab" |
| `04-controller-mouse-event.md` | Slightly more nuanced (security/coord conversion), still small | Likely "let me look at this one" before code |
| **WAIT for responses on 01-04 before filing 05-06** | Establishes tone, lets her gauge bandwidth | — |
| `05-modal-key-discipline.md` | Design discussion, intentionally open | Almost certainly "let me design" |
| `06-multi-file-extensions.md` | Bigger framework change touching loader + manifest format | "Long-term, yes; let me think" |

## What each issue contains

- Title (ready to paste)
- Problem statement
- Concrete API proposal (signature + example usage + sketch impl)
- Use case grounded in the OCR extension
- Workaround the extension author has to use today
- Suggested labels

## Provenance

All six gaps were encountered while adapting
`src/orca/ocr_presenter.py` (and the three sibling modules) from a
perf-branch built-in to a user extension at
`~/.local/share/orca/extensions/ocr.py`. Each gap has a `# GAP-N:`
comment at the corresponding place in the user-extension source
file, so the issue author and Joanie can both trace from
"hypothetical API" to "real line of real code that needs it."

## Patch availability

Per Joanie's email ("touch base before spending a bunch of time"),
**no patches are attached**. Issues 01-04 each have a sketch
implementation in the body that's ~10-20 lines and could be
PR-ready in an hour if she gives the go-ahead. Issues 05-06 are
design proposals only.
