import { describe, expect, it } from "vitest";

import { demoLabel, formatMoney, isZero } from "./money";

describe("formatMoney", () => {
  it("keeps exact decimals without floating point", () => {
    expect(formatMoney({ currency: "INR", amount: "500.00" })).toBe("₹500.00");
    expect(formatMoney({ currency: "INR", amount: "0.1" })).toBe("₹0.10");
    expect(formatMoney({ currency: "USD", amount: "0.000123" })).toBe("$0.000123");
    expect(formatMoney({ currency: "INR", amount: "-3.46" })).toBe("-₹3.46");
  });

  it("groups INR the Indian way and USD in thousands", () => {
    expect(formatMoney({ currency: "INR", amount: "1234567.50" })).toBe("₹12,34,567.50");
    expect(formatMoney({ currency: "USD", amount: "1234567.5" })).toBe("$1,234,567.50");
  });

  it("labels demo money as simulated and treats unknown as unknown", () => {
    expect(demoLabel({ currency: "INR", amount: "25" })).toBe("₹25.00 (simulated)");
    expect(formatMoney(null)).toBe("unknown");
    expect(isZero({ currency: "INR", amount: "0.00" })).toBe(true);
    expect(isZero({ currency: "INR", amount: "0.01" })).toBe(false);
  });
});
