import { Suspense } from "react";

import { DemoCheckout } from "@/features/account/Billing";

export default function Page() {
  return (
    <Suspense fallback={<p role="status">Loading checkout...</p>}>
      <DemoCheckout />
    </Suspense>
  );
}
