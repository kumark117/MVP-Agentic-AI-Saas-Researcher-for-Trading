export const metadata = { title: "About AlloyIQ" };

const SECTIONS: { title: string; body: string[] }[] = [
  {
    title: "What AlloyIQ does",
    body: [
      "AlloyIQ researches metals markets. You choose an instrument, a horizon and a model profile, and three agents gather evidence in parallel: a Price agent (the latest reference quote, and an optional INR view), a Technical agent (indicators such as moving averages, RSI and MACD, calculated by our software, not by the model) and a News agent (recent articles).",
      "A Signal agent then writes a cited report: BUY, SELL, HOLD or NO_SIGNAL, with supporting and opposing evidence, assumptions, risks and limitations. NO_SIGNAL means the evidence does not support a reliable call. It is an honest outcome, not a failure.",
      "AlloyIQ is research only. It places no orders, holds no investment capital and is not investment advice.",
    ],
  },
  {
    title: "Data sources and coverage",
    body: [
      "Every instrument uses reference data: delayed or derived prices and continuous futures series, not a licensed live exchange feed. MCX and LME instruments are not available because no licensed feed is configured.",
      "Quotes can be delayed. Each report shows when its data was observed and how fresh it is for your horizon. Stale data leads to NO_SIGNAL.",
    ],
  },
  {
    title: "How Claude is used",
    body: [
      "The agents use Anthropic's Claude models. The model you choose, and any approved fallback, is shown before you start and recorded with every call.",
      "The model explains numbers but never supplies them. Every number in a report must match a cited evidence value, and reports that fail validation are not shown as signals.",
      "Text from articles is treated as data, never as instructions. Suspicious content is quarantined and excluded.",
    ],
  },
  {
    title: "Simulated payments and real costs",
    body: [
      "Payments are simulated. Demo credit is in INR, has no cash value and cannot be withdrawn. Top-ups, refunds and receipts are demonstrations. No card, bank or UPI details are ever requested, and money collected is always ₹0.00.",
      "The model and data calls are real. Their measured cost is shown separately from the simulated charge, and fixed real-spend limits apply that demo credit can never raise.",
    ],
  },
  {
    title: "Cancellation",
    body: [
      "You can cancel a run at any time, and no new model or tool calls start once the cancellation is seen. Provider work already started may still incur real platform cost. Closing the page does not cancel a run: it keeps going and you can reopen it from any device.",
    ],
  },
  {
    title: "Privacy and retention",
    body: [
      "Your email is never sent to a model. Live progress events are kept for 30 days, detailed activity for 90 days, and reports and history for 12 months. You can request account deletion from the Security page.",
    ],
  },
  {
    title: "Evaluation and limitations",
    body: [
      "Reports are checked automatically for citation support, number grounding and schema. A reviewed evaluation set measures quality over time. Confidence scores are uncalibrated and are not probabilities.",
      "Reference data can be wrong or late, news coverage is limited to short excerpts, and the models can still make mistakes. Check important conclusions against licensed sources.",
    ],
  },
];

export default function About() {
  return (
    <main id="main" className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-10">
      <h1 className="text-3xl font-semibold">About AlloyIQ</h1>
      {SECTIONS.map((s) => (
        <section key={s.title} aria-labelledby={s.title}>
          <h2 id={s.title} className="mb-2 text-xl font-semibold">
            {s.title}
          </h2>
          {s.body.map((p) => (
            <p key={p.slice(0, 32)} className="mb-2">
              {p}
            </p>
          ))}
        </section>
      ))}
    </main>
  );
}
