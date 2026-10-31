# Changelog

## Unreleased

- Use the official GitHub Invertocat for the source link.
- Give the appearance control a persistent Theme label and sun/moon icon, including on mobile; show Follow system inside the menu instead of as the button name.

## 0.4.2 - 2026-10-31

- Replace native appearance popups with consistent themed menus on the login screen and workspace, including keyboard navigation, checked states, and small-screen positioning.
- Put the actual AstroLOG logo in `brand/`, keep its shared ring build input in `brand/source/`, and document stable SVG/PNG links.
- Check that published logo copies stay identical to the canonical web assets.

## 0.4.1 - 2026-10-31

- Display the complete server and relay label with readable type and wrapping on small screens.
- Add an accessible GitHub source link to the workspace header.
- Follow the browser's system appearance by default, including the login screen; remember explicit Light or Dark choices and allow returning to System.
- Preserve reading position when a delayed latest-output request completes after scrolling away.
- Refresh atomic file replacements in place without flashing unchanged rows, and distinguish file modification time from stream heartbeats.
- Expand synthetic browser regressions across desktop/mobile, appearance changes, delayed responses, and live snapshot refreshes.

The existing source deployment can receive the static frontend updates without restarting the viewer or collector. The already-running Python process retains its original runtime version until its next normal restart.
