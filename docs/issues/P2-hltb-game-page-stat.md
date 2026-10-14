# P2: the HLTB stat cell on a game's page (attempted, removed)

The goal: show the cached HowLongToBeat time next to PLAY TIME on a game's
page, the same way the library capsules show it while the HLTB sort is active.

## What was tried

- The agent publishes the shown game's app id (`details.unAppID`, read from the
  details page's React props) through localStorage, because the page's DOM has
  no app link - the "Store Page" tab is a button. **This part proved reliable**
  and can be reused.
- The panel (the Luna window's document) watched the DOM and inserted a
  hand-built cell beside the playtime cell: cloned class names from the real
  stat cells, anchored on whichever stat label existed (Play Time, Last Played,
  Space Required), walked past the tooltip wrapper to the stat row, inserted
  after Play Time or at the end of the row, and learned the icon class from a
  play-time cell so sparse pages matched.

## Why it was removed

- A foreign DOM node inside a React-owned row does not survive: the next
  reconciliation of that subtree rewrites it away. A live DOM probe found the
  cell once (`How Long To Beat` + `29 minutes`) while the user's screen still
  showed nothing - exactly that failure mode.
- The row's shape varies per page (icon-less cells, tooltip wrappers), so
  class-based cloning is fragile even before React interferes.

## What a future attempt needs

- Render a real React child instead of a DOM node: hook the details page's
  jsx/components the same way the library grid is hooked (the agent's wrapper
  already sees every element creation in the shared context), or find a
  supported extension point.
- Keep the app-id bridge idea; it identifies the shown game correctly.
- Acceptance criteria:
  - the cell survives page re-renders and navigating away and back;
  - it shows the main-story hours in Steam's own style, and stays hidden for
    unfetched or `no match` games;
  - the page is untouched when the add-on is absent;
  - verified live on three shapes: a played game (all four stat cells), an
    unplayed game (Space Required only) and a game without cached times.
