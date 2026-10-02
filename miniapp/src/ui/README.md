# Interface package

Screens import controls from this directory. `tokens.css` owns colour, type, spacing, density and motion; `styles.css` owns shared surfaces, control states and the native screen geometry; `desktop.css` owns the desktop column and the value triggers. `components.tsx` owns stateful choices, status and skeletons; `dialogs.tsx` owns temporary layers and their keyboard behavior. Theme preferences override these tokens for the entire application, including terminal colours.

Use existing behavior rather than rendering a second control that performs the same action. A page provides its data and callbacks. It does not implement its own choice menu, notification layer or settings row.

| Block | Contract |
| --- | --- |
| Button | Use `.btn` for an action, `.primary` for the next action, `.danger` for destruction. Height `--ctl-h`, gap 8 px, text 13 px. Disable while submitting; preserve its label. |
| Icon button | Use `.iconbtn` with a translated accessible name and title. 32 px on desktop, 44 px on touch. An icon is not an action until it has a button or link. |
| Input | Use `.field`, a visible label and an adjacent error. Enter submits only an explicit form. Disabled fields retain the value. |
| Text area | Use `.field` for editing a setting; `Composer` for sending messages. Preserve multiline content and a visible focus boundary. |
| Choice | Use `Dropdown` for a long list; `Segmented` for short fixed choices. Composer choices use `ControlTrigger` and an anchored `Popover`. The caret is a separate 14 px icon. In the message toolbar, model and effort share an unboxed group with the caret at its end; other value controls retain their boundary. |
| Checkbox | A selection within a set uses a native labelled checkbox. Space toggles it; disabled is explicit. Do not use it for an action. |
| Switch | `Switch` applies a binary setting immediately; its accessible name is the setting, its checked state is the value. |
| Menu | `OverflowMenu` contains actions; context menus invoke these same commands at the pointer, clamped 8 px inside the viewport. Shift+F10 opens the focused element's menu. Arrow keys move, Enter chooses, Escape closes and restores focus. Do not replace a menu with a confirmation dialog. |
| List | Keep its own scroll container. Empty and loading states replace its body, never the navigation. Group headings are 12 px; ordinary row titles are 14 px. |
| List row | 36 px for one line, up to 52 px for title and metadata. A selected notification opens its detail beside the list on a wide desktop and in the row on a smaller window. |
| Tabs | Use the existing tablist components with a selected state and keyboard handling. Scroll a long tab strip; do not scroll the page horizontally. |
| Badge | Count or state only, 11 px text. It never duplicates an action. Do not communicate failure through colour alone. |
| Toast | `toast` announces a result; use Undo only when the operation is actually reversible. A persistent failure belongs by its affected control. |
| Modal | `Sheet` for a task, `confirmDialog` for a consequential action. Tab stays inside; Escape closes only the top layer; focus returns to its opener. |
| Popover | `Popover` stays within the viewport, chooses above or below the opener and repositions on resize. Model choice starts with provider groups, local vector marks and model counts. A group shows only its own presets; Back or ArrowLeft returns to groups, Escape closes. Search at the root spans all providers; search inside a group stays scoped. Custom model entry is an explicit expandable action. Arrow editing stays in the search field and ArrowDown enters choices. |
| Banner | Use the existing offline, maintenance, fallback and change strips. Keep actionable failures readable until resolved. |
| Empty state | Say what appears here and offer one next action. A sidebar that already has New does not repeat the same button in its empty body. |
| Skeleton | `Skeleton` reserves the expected row geometry while loading. Keep retry distinct from loading. Motion respects the application and OS preference. |
| Agent activity | Preserve the existing compact hierarchical tree, grouped calls, detail expansion and result previews. Do not turn each tool call into a large card. |
| File diff | Preserve the native file diff, line numbers, context and expansion. Code uses `--mono` and `--fs-mono`; readable content may scroll within the diff. |
| Sidebar | A 52 px icon rail remains beside the 272 px contextual list. The list starts directly with its header, New and projects; never put a destination grid above it. Drag the single outer edge to resize the list between 232 and 360 px. Folded width is 52 px. The selection and width persist across routes. |
| Header | `PageHeader` names the page and its actions. Do not add a second row of metadata to conversation headers. |
| Command palette | Existing `Palette`: search destinations and actions, arrow navigation, Enter to choose, Escape to close. Keep it separate from searching conversation content. |
| Message field | Existing `Composer`: one common width with the timeline, text above controls, mode on the left, model/effort on the right within the shared field boundary, attachments and voice progress, primary send/stop action with an 8 px radius. Idle voice and enabled Send share the trailing position. The field uses 12 px insets, an 8 px gap and a 40 px minimum text area. Retain draft, approval, questions and queued steer behavior. |

Defaults use the named theme already chosen by the operator. Hover adds contrast, keyboard focus is a 2 px accent outline, and disabled controls cannot activate. Error states retain the attempted value. Loading controls do not move neighboring controls. Use 4/8/12/16/24 px spacing steps and the 13/14 px UI type scale; prose keeps its separate 15/16 px scale.

At half a desktop window, retain complete text and usable controls. Panels and lists scroll internally; popovers stay inside the viewport. Reduced motion disables nonessential animation without delaying input. The optional native companion is off until enabled in Appearance; it receives a small status enum and never receives credentials or conversation text.
