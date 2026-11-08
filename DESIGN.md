# TG Agent Workspace

## 1. Atmosphere and identity
A dark-first control desk wearing the visual language of a modern Telegram client
(Telegram Desktop / AyuGram): a near-dark desaturated canvas, soft elevated
surfaces, rounded 12-16px components, and a correspondence pane that reads like a
real messenger with message bubbles. One named accent — Telegram Blue — carries
links, active navigation and primary actions. The workspace is still a
project-control desk, not a chat client: the attention ledger, registers and
evidence drawer remain the product; only the skin is messenger-grade.

References: AyuGram Desktop demo screenshots (public repo) for the vibe — dark
canvas, grouped settings rows, rounded toggles, blue section headers — not for
pixels. No marketing illustration, no hero, no external assets.

## 2. Color
Dark is the default theme; light remains available in settings and via the system
preference. One accent: Telegram Blue #3390EC.

| Role | Token | Dark (default) | Light |
|---|---|---|---|
| Canvas | --canvas | #0e1621 | #ffffff |
| Navigation | --nav | #17212b | #ffffff |
| Surface | --surface | #1c2733 | #ffffff |
| Soft surface | --soft | #232e3c | #f1f4f7 |
| Ink | --ink | #f5f7fa | #0f1720 |
| Secondary text | --muted | #8a9baa | #5c6b7a |
| Rules | --rule | #26333f | #e3e8ed |
| Edge (control border) | --edge | #3a4a5a | #c9d3dc |
| Accent | --accent | #3390ec | #3390ec |
| Accent strong (filled controls) | --accent-strong | #2c74b8 | #1f6fb2 |
| Accent background | --accent-soft | #1e3a5f | #e3f0fd |
| Accent control ink | --on-accent | #ffffff | #ffffff |
| Danger | --danger | #ff7a7a | #c0392b |
| Danger background | --danger-soft | #3a2226 | #fce9e9 |
| Warning | --warning | #f0b429 | #8a6100 |
| Warning background | --warning-soft | #3a3220 | #fbf0d9 |
| Scrim | --scrim | #00000099 | #0f172055 |

Message bubbles carry a per-sender tint (six neutral steps, --bubble-0..5) derived
from a hash of the sender name, so a group chat reads as distinct voices without
avatars. Neutral hairlines separate register rows. No decorative accent stripes,
accent card outlines or gradients. Status always has text as well as color.

## 3. Typography
One family: locally available Segoe UI Variable / Segoe UI / sans-serif, so private
project data needs no external font request. No serif. Numbers use tabular figures.
Scale: --text-xs 12px (metadata only), --text-sm 14px, --text-base 15px,
--text-lg 18px, --text-title clamp(22px,2.4vw,28px). Mobile body 17px, secondary
15px. Weights 400/500/600/700; body line-height 1.5; headings 1.2. Prose max 70ch.

## 4. Spacing and layout
Four-pixel base: --s1 4px, --s2 8px, --s3 12px, --s4 16px, --s5 20px,
--s6 24px, --s8 32px, --s10 40px. Radii: --r1 8px, --r2 12px, --r3 16px.
Desktop labelled sidebar --sidebar 224px, fluid main content max 1500px.
Main owns vertical scroll; shell bounded to 100dvh. Drawer owns a separate scroll.
At 900px nav becomes an explicit menu; at 600px registers become stacked rows and
filters wrap. Primary content never needs horizontal scrolling at 390px.
No permanent chat/topic/message columns. Correspondence has one source tree and
a fluid message panel, single pane on mobile. Drawer width min(520px,100vw).

## 5. Components and states
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
