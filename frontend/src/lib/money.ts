/**
 * Money display. Amounts arrive as exact decimal strings (D080) and are never converted
 * to binary floats for arithmetic; formatting works on the string itself.
 */
export type Money = { currency: "INR" | "USD"; amount: string };

const SYMBOL: Record<Money["currency"], string> = { INR: "₹", USD: "$" };

function group(intPart: string, currency: Money["currency"]): string {
  if (currency === "INR") {
    // Indian grouping: 12,34,567
    const last3 = intPart.slice(-3);
    const rest = intPart.slice(0, -3);
    return rest ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3 : last3;
  }
  return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Format with at least `minDecimals` and the exact stored precision beyond that. */
export function formatMoney(m: Money | null | undefined, minDecimals = 2): string {
  if (!m) return "unknown";
  const neg = m.amount.startsWith("-");
  const raw = neg ? m.amount.slice(1) : m.amount;
  const [i = "0", f = ""] = raw.split(".");
  const trimmed = f.replace(/0+$/, "");
  const frac = trimmed.length >= minDecimals ? trimmed : trimmed.padEnd(minDecimals, "0");
  const intPart = i.replace(/^0+(?=\d)/, "") || "0";
  return `${neg ? "-" : ""}${SYMBOL[m.currency]}${group(intPart, m.currency)}${frac ? "." + frac : ""}`;
}

/** Demo INR values always carry the "simulated" label in the UI (section 15). */
export function demoLabel(m: Money | null | undefined): string {
  return `${formatMoney(m)} (simulated)`;
}

export function isZero(m: Money | null | undefined): boolean {
  return !!m && /^-?0*(\.0*)?$/.test(m.amount);
}
