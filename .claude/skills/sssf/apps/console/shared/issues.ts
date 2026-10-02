/** The Tracker a Local Markdown repo's `--list-ready` names (issues.LOCAL). */
export const LOCAL_TRACKER = "Local Markdown";

/** A local issue's repo-relative path, as issues._LOCAL_PATH matches it. */
const LOCAL_PATH = /^\.scratch\/[^/]+\/(?:spec\.md|issues\/\d+-[^/]*\.md)$/;

/**
 * The issue a prompt names, as the reference that keys it: `#42` for `#42`
 * or the issue's URL, and in a Local Markdown repo a local issue's path,
 * normalised as issues.local_ref does — or null for any other prompt. It only
 * recognises the reference, mirroring issues.parse_ref and local_ref; whether
 * the issue may run stays in Python (ADR 0001). Which Tracker the repo uses is
 * Python's to say too, through the listing: in a GitHub repo the same path is
 * a request file. Shared, so the server's hold on an issue and the form's
 * read-only note agree on what is an issue Launch.
 */
export function issueNamed(prompt: unknown, tracker: string | null): string | null {
  if (typeof prompt !== "string") return null;
  const text = prompt.trim();
  const ref = /^#(\d+)$/.exec(text) ?? /^https?:\/\/\S+\/issues\/(\d+)\/?(?:[?#]\S*)?$/.exec(text);
  if (ref) return `#${Number(ref[1])}`;
  if (tracker !== LOCAL_TRACKER) return null;
  const path = text.replaceAll("\\", "/").replace(/^\.\//, "");
  return LOCAL_PATH.test(path) ? path : null;
}

/** How a listed issue is named, and passed back to an ADW: `#42`, or its path (Issue.ref). */
export function issueRef(issue: { number: number | null; path: string | null }): string {
  return issue.path ?? `#${issue.number}`;
}

/**
 * The ADW that claimed a Run's issue, from the Run's `adw_name` ("adw_a + adw_b"):
 * its first, since only a committing, non-resuming ADW claims, and that one
 * starts the Run. A Rerun runs it again.
 */
export function claimingAdw(adwName: string | null | undefined): string | null {
  return adwName?.split(" + ")[0] || null;
}
