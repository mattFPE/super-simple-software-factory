/**
 * The issue a prompt names, as `#42` or the issue's URL — the two references
 * issues.py takes — or null for any other prompt. It only recognises the
 * reference, mirroring issues.parse_ref; whether the issue may run stays in
 * Python (ADR 0001). Shared, so the server's hold on an issue and the form's
 * read-only note agree on what is an issue Launch.
 */
export function issueNamed(prompt: unknown): number | null {
  if (typeof prompt !== "string") return null;
  const text = prompt.trim();
  const ref = /^#(\d+)$/.exec(text) ?? /^https?:\/\/\S+\/issues\/(\d+)\/?(?:[?#]\S*)?$/.exec(text);
  return ref ? Number(ref[1]) : null;
}

/**
 * The ADW that claimed a Run's issue, from the Run's `adw_name` ("adw_a + adw_b"):
 * its first, since only a committing, non-resuming ADW claims, and that one
 * starts the Run. A Rerun runs it again.
 */
export function claimingAdw(adwName: string | null | undefined): string | null {
  return adwName?.split(" + ")[0] || null;
}
