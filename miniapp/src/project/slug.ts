// A project's name as the name of its new folder: "Умный дом" becomes umnyj-dom.
//
// The folder used to be named by the project's id, a hash nobody could find again in a file manager.
// Cyrillic is spelled in Latin letters rather than kept, because the folder is also a path the agents
// type into shells, git and compose files, where a Cyrillic path is a quoting accident waiting to
// happen. The server checks the result again; this only proposes it.

const LETTERS: Record<string, string> = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i", й: "j", к: "k", л: "l",
  м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t", у: "u", ф: "f", х: "h", ц: "c", ч: "ch", ш: "sh",
  щ: "shch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya", і: "i", ї: "yi", є: "ye", ґ: "g",
};

/** The longest folder name proposed; the server takes up to 80, and a path is read in a breadcrumb. */
export const SLUG_MAX = 48;

/** A folder name for a project name: lower case, Latin letters, digits and single hyphens. A name with
 *  nothing spellable in it ("???") becomes "project". */
export function slugify(name: string): string {
  const spelled = [...name.toLowerCase()].map((ch) => LETTERS[ch] ?? ch).join("");
  const plain = spelled.normalize("NFKD").replace(/[̀-ͯ]/g, "");
  const slug = plain.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, SLUG_MAX).replace(/-+$/g, "");
  return slug || "project";
}

/** Whether a typed folder name is one the server will take: one segment, nothing hidden, no separator. */
export function folderNameProblem(name: string): boolean {
  const value = name.trim();
  return !value || value.length > 80 || value.startsWith(".") || /[/\\:\u0000-\u001f]/.test(value);
}

/** The next free name beside the taken ones: umnyj-dom, umnyj-dom-2, umnyj-dom-3… */
export function freeName(base: string, taken: (name: string) => boolean): string {
  if (!taken(base)) return base;
  for (let n = 2; n < 100; n++) if (!taken(`${base}-${n}`)) return `${base}-${n}`;
  return `${base}-${Date.now().toString(36)}`;
}
