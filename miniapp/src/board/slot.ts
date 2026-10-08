// Where the review page on a phone puts its decision footer. The result's own component decides what
// the footer holds (Return, Accept, Merge and why each waits), but on a phone the footer stays at the
// bottom of every tab of the page, Task, Result and Diff alike, so the page hands the component an
// element to draw it into rather than the component drawing it in its own place in the Result tab.
// Null everywhere else, where the component keeps its actions in its own flow.

import { createContext } from "react";

export const DecisionSlot = createContext<HTMLElement | null>(null);
