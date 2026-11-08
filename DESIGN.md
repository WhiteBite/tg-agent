# TG Agent Workspace

## 1. Atmosphere and identity
A dark-first control desk in the visual language of Huly (see `huly-ref.md`): a
warm near-black graphite canvas, layered graphite surfaces, a narrow chromatic band
of two accents — Electric Iris (#5683da) for the single primary action and active
navigation, Ember Pulse (#ff8964) as warm punctuation in pills, dots and the
"expiring" metric — and pill-first geometry (9999px controls, 12px cards). One
named UI face (Inter, with a system fallback so no external font is requested).
The workspace stays a project-control desk: the attention ledger, registers and
evidence drawer remain the product; only the skin is Huly-grade.

Reference: `huly-ref.md` (Huly style reference — palette, type scale, radii,
shadows, do/don'ts). Tokens are adapted, not copied; no marketing hero, no aurora
as a full background, no external assets.

## 2. Color
Dark is the default theme; light remains available in settings and via the system
preference. One cool accent (Electric Iris) + one warm accent (Ember Pulse) are the
entire chromatic vocabulary — no third accent.

| Role | Token | Dark (default) | Light |
|---|---|---|---|
| Canvas | --canvas | #0e0f11 | #ffffff |
| Navigation | --nav | #131418 | #ffffff |
| Surface | --surface | #17181c | #ffffff |
| Soft surface | --soft | #1f2025 | #f6f6f6 |
| Ink | --ink | #f4f5f7 | #050506 |
| Secondary text | --muted | #95979e | #6b6c6d |
| Rules | --rule | #24262b | #e5e5e7 |
| Edge (control border) | --edge | #4a4b50 | #d1d1d1 |
| Accent (iris) | --accent | #5683da | #5683da |
| Accent strong (filled) | --accent-strong | #4a76cc | #4a76cc |
| Accent background | --accent-soft | #1b2740 | #e8eefb |
| Accent control ink | --on-accent | #ffffff | #ffffff |
| Warm accent (ember) | --ember | #ff8964 | #e0744a |
| Warm accent background | --ember-soft | #3a2118 | #fdece4 |
| Danger | --danger | #ff6b6b | #c0392b |
| Danger background | --danger-soft | #3a1f1f | #fce9e9 |
| Warning (= warm accent) | --warning | #ff8964 | #c96a3f |
| Warning background | --warning-soft | #3a2118 | #fbe6dc |
| Scrim | --scrim | #000000aa | #0f172055 |

Message bubbles carry a per-sender tint (six neutral steps, --bubble-0..5) derived
from a hash of the sender name, so a group chat reads as distinct voices without
avatars. Neutral hairlines separate register rows. No decorative accent stripes,
accent card outlines or gradients. Status always has text as well as color.
Elevation is carried by tinted shadows, never heavier borders: --shadow-1 for raised
chrome (topbar, toast), --shadow-2 for overlays (drawer, modal). Reply quotations and
blockquotes use a neutral 2px rule in --edge, never the accent.

## 3. Typography
One UI face: `Inter` first in the stack, with a local system fallback
(`ui-sans-serif, system-ui, "Segoe UI", Roboto`), so no external font is requested
and private data stays local. No serif. Numbers use tabular figures. Scale follows
the Huly reference: --text-xs 11px (caption/metadata), --text-sm 14px (body),
--text-base 14px, --text-lg 18px (subheading), --text-title clamp(22px,2.2vw,28px)
(heading-sm). Weights 300/400/500/600/700 (500–600 for emphasis, 300 sparingly for
quiet metadata); body line-height 1.5; headings 1.25. Letter spacing tightens with
size (-0.01em body, -0.02em subhead). Prose max 70ch.

## 4. Spacing and layout
Four-pixel base: --s1 4px, --s2 8px, --s3 12px, --s4 16px, --s5 20px,
--s6 24px, --s8 32px, --s10 40px. Radii: --r1 8px (inputs/fields), --r2 12px
(cards/panels), --r3 16px (overlays), --radius-pill 9999px — all buttons, tabs and
tags are pills (the signature shape). Desktop labelled sidebar --sidebar 224px,
fluid main content max 1500px.
Main owns vertical scroll; shell bounded to 100dvh. Drawer owns a separate scroll.
At 900px nav becomes an explicit menu; at 600px registers become stacked rows and
filters wrap. Primary content never needs horizontal scrolling at 390px.
No permanent chat/topic/message columns. Correspondence has one source tree and
a fluid message panel, single pane on mobile. Drawer width min(520px,100vw).

## 5. Components and states
- Pill-first: buttons, tabs and tags use --radius-pill; cards/panels use --r2;
  inputs use --r1. No sharp corners on controls.
- Shadows: dark surfaces get depth from hairline borders (--rule/--edge) and tonal
  layering, not drop shadows; shadows are reserved for floating overlays only
  (drawer, modal, toast).
- App shell: labelled nav links, current item a filled accent pill + aria-current,
  header, main and skip link.
- Page header: breadcrumb/back, title, short explanation and one primary action.
- Register: semantic table, sortable headers, clickable title buttons, owner/due/status,
  source and review/integration labels. Mobile uses the same content in a stacked grid.
- Filters: visible labels, project/owner/status/type/date selectors, search scope visible.
- Status badge: compact rounded pill, localized text; open/in progress/blocked/done.
- Buttons: primary filled accent-strong, secondary neutral surface, tertiary text,
  destructive red; 44px mobile. Always hover, pressed, disabled, focus-visible and
  busy states; no hover-only actions.
- Correspondence: source tree of chats/topics; message pane of bubbles. Each bubble
  has an initials circle, sender name in accent, body and a time/actions foot.
  Consecutive messages from one sender collapse the avatar and name. Selection is a
  tonal wash plus an explicit action label, never an accent outline.
- Entity drawer: modal dialog, explicit close, Escape, initial heading focus, trap tab,
  return focus on close, audit timeline, source quotations, review fields and errors.
- Evidence block: author/date, escaped quote, missing/deleted marker, open-source control.
- Empty/error state: cause plus usable next action; never fake totals or successful sync.
- Project row: actual project counts, changes cursor, independent mark-reviewed action.
- Integration panel: unconfigured/unverified labels, masked configuration presence only,
  local payload preview and blocking reasons; no send button or send endpoint.
- Settings forms: project-topic assignment, people aliases, local notification preview.
- Loading state: immediate labelled busy state, errors remain near failed content.
- Search view: separate entity/message results and pagination, context navigation.
- Chat analysis view: one topic scoped summary — watch/project line, latest digests,
  kind counts, entity register reusing the shared drawer, gaps with reasons, recent
  messages as bubbles; deep-linked by chat_id and topic_id for external clients.

Primitives are shared CSS classes and JS renderers under ui_assets, not parallel
framework components. SVG line icons use one 24x24 viewBox and 1.75 stroke,
rendered at 20px.

## 6. Motion and interaction
120ms hover/press, 180ms drawer opacity/transform, no layout animation. Reduced motion
disables positional transitions. Navigation is URL-hash based with query fields, so
reload/back preserves views. Saved views and recent searches live locally in browser
storage; human entity review and project review cursors persist in SQLite metadata.

## 7. Accessibility
Named inputs and icon controls, semantic landmarks/table/dialog, visible 2px focus
ring, Escape dismissal and focus restoration, 44px touch controls, selectable evidence,
overflow-wrap:anywhere for long source strings. Dark is the default theme; light and
system remain available in settings. Body text meets 4.5:1 on both themes; filled
controls use the accent-strong step so their white label also clears 4.5:1. Do not
express urgency only through red ink. No external script/CDN/font/image assets.

## 8. Constraints and accepted debt
Keep stdlib HTTP, existing guards, masking, MCP and Telegram functions. Existing
manage/announce additions are unrelated and must remain unchanged. The UI never
contacts 101 or sends Telegram notifications automatically. Credentials configured
does not mean API contract verified. Review rejection is local human-review metadata;
the exporter does not yet enforce review decisions and the UI must say so explicitly.
SQLite schema v5 remains unchanged; workspace-local metadata uses ws: keys. No
fictitious pending/error export state: only observations from integrations/events.
Full 101 task field/status/project/assignee mapping still requires a real contract.

## QA contract
Attention/project/list/detail/evidence/review/search/setup and 101 preview on seeded
database; empty/missing/deleted/long content; 390px and 1440px dark/light; keyboard;
existing correspondence pagination/FTS/watch/alias/pins/copy/sync; HTTP guards and
redaction. Screenshots and independent review are required, not just test counts.
