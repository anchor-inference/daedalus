// One configured model chosen from a list that opens over the page, as the value at the end of a
// settings row: its name and a caret, no frame.
//
// `ModelPicker` is the long form (a searchable list drawn in place under its own row, with the
// provider's name, the window and the price on each line) and stays where a choice among many models
// is the point of the page. A row that only names which model does a background job reads better as
// a value than as a block of its own, so this takes the same catalogue and the same empty choice and
// draws them as a `Select`.

import { t } from "./i18n";
import { modelItems, type ModelCatalogue } from "./modelitems";
import { Select } from "./ui/primitives";

export function ModelChoice({ id, label, catalogue, value, onChange, defaultOption, disabled }: {
  id?: string;
  label: string;
  catalogue: ModelCatalogue;
  value: string;
  onChange: (next: string) => void;
  /** The empty choice: "the session's own model", "automatic". */
  defaultOption?: { label: string };
  disabled?: boolean;
}) {
  const { items } = modelItems(catalogue);
  const options = [
    ...(defaultOption ? [{ id: "", label: defaultOption.label }] : []),
    // A broken or slow model says so in its name, in the list and as the chosen value, so a bad choice is seen before it is made.
    ...items.map((item) => ({ id: item.id, label: item.alert ? `${item.label} · ${item.alert.word}` : item.label, hint: [item.alert?.why, item.detail].filter(Boolean).join(" · ") || undefined })),
  ];
  // A model chosen here and deleted since stays in the configuration; the value still names it, as
  // missing, rather than showing the first option and letting the operator think it is in use.
  const shown = !value || options.some((option) => option.id === value) ? options : [{ id: value, label: `${value} · ${t("sel.missing")}`, hint: undefined }, ...options];
  return <Select<string> id={id} className="set-value" label={label} value={value} options={shown} onChange={onChange} disabled={disabled} />;
}
