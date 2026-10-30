/** Only same-app absolute paths are accepted as post-login destinations. Anything else (another
 * origin, protocol-relative `//host`, backslash tricks, the auth pages themselves) becomes "/". */
export function safeRedirect(target: unknown): string {
  if (typeof target !== "string") return "/";
  if (!target.startsWith("/") || target.startsWith("//") || target.includes("\\")) return "/";
  if ([...target].some((ch) => ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f)) return "/";
  if (target === "/login" || target === "/signup") return "/";
  return target;
}
