# TG Agent Workspace

## 1. Atmosphere and identity
A project-control desk, not another messenger. Light paper surfaces, deep ink headings,
restrained teal controls, compact readable registers. The signature is a continuous
attention ledger that opens evidence beside the work rather than replacing it.
The existing Telegram browser remains an explicit Correspondence destination.

References: existing ui.html and its watch/message workflows; frontend redesign-skill,
layout-skill and design-system-architecture references. This is a redesign, not an
exact clone or generated-image implementation. No marketing illustration or hero.

## 2. Color
| Role | Token | Light | Dark |
|---|---|---|---|
| Canvas | --canvas | #f5f5f0 | #171c1b |
| Navigation | --nav | #eeefe8 | #1c2321 |
| Surface | --surface | #ffffff | #222b28 |
| Soft surface | --soft | #f5f7f3 | #293330 |
| Ink | --ink | #202d28 | #ecf0eb |
| Secondary text | --muted | #58665e | #aebbb2 |
| Rules | --rule | #d9dfd6 | #3e4c45 |
| Accent | --accent | #17654e | #83c9ac |
| Accent background | --accent-soft | #e1efe6 | #293f34 |
| Accent control ink | --on-accent | #ffffff | #14251b |
| Danger | --danger | #a63131 | #ffb2aa |
| Danger background | --danger-soft | #fbebe6 | #422b28 |
| Warning | --warning | #825817 | #edce85 |
| Warning background | --warning-soft | #f6eedc | #3e3726 |
| Scrim | --scrim | #18282055 | #070d0a88 |
Neutral hairlines separate register rows. No decorative accent stripes, accent
card outlines, or gradients. Status always has text as well as color.

## 3. Typography
Body uses locally available Segoe UI Variable / Segoe UI / sans-serif so private
project data does not require external font requests. Page titles use Georgia / serif
for an editorial ledger identity. Exactly two families. Numbers use tabular figures.
Scale: --text-xs 12px (metadata only), --text-sm 14px, --text-base 15px,
--text-lg 18px, --text-title clamp(26px,3vw,34px). Mobile body 17px, secondary 15px.
Weights 400/500/600/700; body line-height 1.5; headings 1.15. Prose max 70ch.

## 4. Spacing and layout
Four-pixel base: --s1 4px, --s2 8px, --s3 12px, --s4 16px, --s5 20px,
--s6 24px, --s8 32px, --s10 40px. Radii: --r1 6px, --r2 10px, --r3 16px.
Desktop labelled sidebar --sidebar 224px, fluid main content max 1500px.
Main owns vertical scroll; shell bounded to 100dvh. Drawer owns a separate scroll.
At 900px nav becomes an explicit menu; at 600px registers become stacked rows and
filters wrap. Primary content never needs horizontal scrolling at 390px.
No permanent chat/topic/message columns. Correspondence has one source tree and
a fluid message panel, single pane on mobile. Drawer width min(520px,100vw).

## 5. Components and states
- App shell: labelled nav links, current wash + aria-current, header, main and skip link.
- Page header: breadcrumb/back, editorial title, short explanation and one primary action.
- Register: semantic table, sortable headers, clickable title buttons, owner/due/status,
  source and review/integration labels. Mobile uses the same content in a stacked grid.
- Filters: visible labels, project/owner/status/type/date selectors, search scope visible.
- Status badge: compact rounded rectangle, localized text; open/in progress/blocked/done.
- Buttons: primary teal, secondary neutral, tertiary text, destructive red; 44px mobile.
  Always hover, pressed, disabled, focus-visible and busy states; no hover-only actions.
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
  messages; deep-linked by chat_id and topic_id for external clients.

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
overflow-wrap:anywhere for long source strings. Light default; dark and system available
in settings. Do not express urgency only through red ink. No external script/CDN assets.

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
database; empty/missing/deleted/long content; 390px and 1440px light/dark; keyboard;
existing correspondence pagination/FTS/watch/alias/pins/copy/sync; HTTP guards and
redaction. Screenshots and independent review are required, not just test counts.
