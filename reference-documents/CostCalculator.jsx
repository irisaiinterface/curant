import React, { useState, useMemo } from "react";

const fmt = (n) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

const fmt0 = (n) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 0 });

function Slider({ label, value, onChange, min, max, step, suffix, hint }) {
  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <label style={{ fontFamily: "system-ui, -apple-system, sans-serif", fontSize: 13, color: "#5a5548", letterSpacing: "0.01em" }}>
          {label}
        </label>
        <span style={{ fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace", fontSize: 15, color: "#1a1814", fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
          {value}{suffix}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: "100%", accentColor: "#8a6d4f", height: 4 }}
      />
      {hint && (
        <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, color: "#9a9488", marginTop: 3 }}>{hint}</div>
      )}
    </div>
  );
}

function LedgerRow({ label, value, muted, indent }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        padding: "7px 0",
        borderBottom: "1px solid #eae6dd",
        paddingLeft: indent ? 14 : 0,
      }}
    >
      <span style={{ fontFamily: "system-ui, sans-serif", fontSize: 13, color: muted ? "#9a9488" : "#3a3630" }}>
        {label}
      </span>
      <span style={{ fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace", fontSize: 13, color: muted ? "#9a9488" : "#1a1814", fontVariantNumeric: "tabular-nums" }}>
        {value}
      </span>
    </div>
  );
}

export default function CostCalculator() {
  const [customers, setCustomers] = useState(50);
  const [price, setPrice] = useState(35);
  const [avgSms, setAvgSms] = useState(150);
  const [voicePct, setVoicePct] = useState(40);
  const [freeMinutes, setFreeMinutes] = useState(30);
  const [avgVoiceMinutes, setAvgVoiceMinutes] = useState(45);
  const [vapiCostPerMin, setVapiCostPerMin] = useState(0.2);
  const [subsidyPct, setSubsidyPct] = useState(50);
  const [workspacePct, setWorkspacePct] = useState(30);
  const [workspaceCost, setWorkspaceCost] = useState(3);
  const [vpsMonthly, setVpsMonthly] = useState(15);

  const calc = useMemo(() => {
    const telnyxNumber = 1.0;
    const telnyxSmsAddon = 0.1;
    const smsUsage = avgSms * 0.006; // blended Telnyx segment + carrier surcharge estimate
    const tenDlcCampaign = 10; // flat, shared across all customers
    const tenDlcBrandAmortized = 4.5 / 12; // one-time $4.50, spread over a year

    const voiceCustomers = customers * (voicePct / 100);
    const overageMinutes = Math.max(0, avgVoiceMinutes - freeMinutes);
    const freeMinutesCost = freeMinutes * vapiCostPerMin; // owner pays full cost for the free bundle
    const overageOwnerShare = overageMinutes * vapiCostPerMin * (subsidyPct / 100);
    const overageCustomerShare = overageMinutes * vapiCostPerMin * (1 - subsidyPct / 100);
    const voiceCostPerVoiceCustomer = freeMinutesCost + overageOwnerShare;

    const workspaceCostPerCustomer = (workspacePct / 100) * workspaceCost;

    const stripeFee = price * 0.029 + 0.3;

    const fixedPerCustomer = telnyxNumber + telnyxSmsAddon + smsUsage + workspaceCostPerCustomer + stripeFee;
    const avgVoiceCostPerCustomer = (voicePct / 100) * voiceCostPerVoiceCustomer;
    const totalCostPerCustomer = fixedPerCustomer + avgVoiceCostPerCustomer;

    const revenuePerCustomer = price + (voicePct / 100) * overageCustomerShare;
    const marginPerCustomer = revenuePerCustomer - totalCostPerCustomer;

    const totalFixedInfra = vpsMonthly + tenDlcCampaign + tenDlcBrandAmortized;
    const totalRevenue = revenuePerCustomer * customers;
    const totalCost = totalCostPerCustomer * customers + totalFixedInfra;
    const totalMargin = totalRevenue - totalCost;

    return {
      telnyxNumber, telnyxSmsAddon, smsUsage, tenDlcCampaign, tenDlcBrandAmortized,
      voiceCostPerVoiceCustomer, workspaceCostPerCustomer, stripeFee,
      totalCostPerCustomer, revenuePerCustomer, marginPerCustomer,
      totalFixedInfra, totalRevenue, totalCost, totalMargin,
      overageCustomerShare, voiceCustomers,
    };
  }, [customers, price, avgSms, voicePct, freeMinutes, avgVoiceMinutes, vapiCostPerMin, subsidyPct, workspacePct, workspaceCost, vpsMonthly]);

  const marginColor = calc.totalMargin >= 0 ? "#3d6b4f" : "#a34a3a";

  return (
    <div style={{ background: "#faf8f4", minHeight: "100vh", padding: "24px 18px 60px" }}>
      <div style={{ maxWidth: 480, margin: "0 auto" }}>
        <div style={{ marginBottom: 28 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.12em", textTransform: "uppercase", color: "#9a8560", marginBottom: 4 }}>
            Curant Cloud — Ledger
          </div>
          <h1 style={{ fontFamily: "system-ui, sans-serif", fontSize: 24, fontWeight: 700, color: "#1a1814", margin: 0, letterSpacing: "-0.01em" }}>
            Monthly cost model
          </h1>
        </div>

        {/* Summary — the number that matters most, up top */}
        <div style={{ background: "#1a1814", borderRadius: 10, padding: "20px 20px 18px", marginBottom: 28 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: "#c9bfa8", marginBottom: 6 }}>
            Total monthly margin, at {customers} customers
          </div>
          <div style={{ fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace", fontSize: 34, fontWeight: 700, color: calc.totalMargin >= 0 ? "#7fd99a" : "#e08a78", letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums" }}>
            {fmt0(calc.totalMargin)}
          </div>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 12, color: "#8a8378", marginTop: 6 }}>
            {fmt(calc.revenuePerCustomer)} revenue − {fmt(calc.totalCostPerCustomer)} cost = {fmt(calc.marginPerCustomer)} per customer
          </div>
        </div>

        {/* Inputs */}
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "#9a8560", marginBottom: 14, borderTop: "1px solid #e5ddc9", paddingTop: 20 }}>
            Scale & pricing
          </div>
          <Slider label="Customers" value={customers} onChange={setCustomers} min={1} max={500} step={1} suffix="" />
          <Slider label="Subscription price" value={price} onChange={setPrice} min={20} max={80} step={1} suffix="/mo" />
          <Slider label="Avg SMS messages / customer / mo" value={avgSms} onChange={setAvgSms} min={0} max={500} step={10} suffix="" />
        </div>

        <div style={{ marginBottom: 8 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "#9a8560", marginBottom: 14, borderTop: "1px solid #e5ddc9", paddingTop: 20 }}>
            Voice (Vapi)
          </div>
          <Slider label="% of customers using voice" value={voicePct} onChange={setVoicePct} min={0} max={100} step={5} suffix="%" />
          <Slider label="Free minutes included / mo" value={freeMinutes} onChange={setFreeMinutes} min={0} max={120} step={5} suffix=" min" />
          <Slider label="Avg minutes used (voice customers)" value={avgVoiceMinutes} onChange={setAvgVoiceMinutes} min={0} max={200} step={5} suffix=" min" />
          <Slider label="Vapi cost / minute (all-in)" value={vapiCostPerMin} onChange={setVapiCostPerMin} min={0.05} max={0.4} step={0.01} suffix="/min"
            hint="Platform fee alone is $0.05/min — $0.15–0.33 all-in with STT/LLM/TTS, per Vapi's own current numbers" />
          <Slider label="Your subsidy on overage minutes" value={subsidyPct} onChange={setSubsidyPct} min={0} max={100} step={5} suffix="%"
            hint="You cover this %; the customer is billed the rest" />
        </div>

        <div style={{ marginBottom: 8 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "#9a8560", marginBottom: 14, borderTop: "1px solid #e5ddc9", paddingTop: 20 }}>
            Google Workspace utility email
          </div>
          <Slider label="% of customers with it enabled" value={workspacePct} onChange={setWorkspacePct} min={0} max={100} step={5} suffix="%" />
          <Slider label="Cost per seat" value={workspaceCost} onChange={setWorkspaceCost} min={2.5} max={8.4} step={0.1} suffix="/mo"
            hint="$2.50–3 via an authorized distributor, $7–8.40 direct from Google" />
        </div>

        <div style={{ marginBottom: 24 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "#9a8560", marginBottom: 14, borderTop: "1px solid #e5ddc9", paddingTop: 20 }}>
            Fixed infrastructure
          </div>
          <Slider label="VPS hosting" value={vpsMonthly} onChange={setVpsMonthly} min={5} max={100} step={5} suffix="/mo"
            hint="Shared across all customers, not per-customer" />
        </div>

        {/* Ledger breakdown */}
        <div style={{ background: "#fff", borderRadius: 10, padding: "18px 18px 8px", border: "1px solid #eae6dd" }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "#9a8560", marginBottom: 10 }}>
            Per-customer cost breakdown
          </div>
          <LedgerRow label="Telnyx phone number" value={fmt(calc.telnyxNumber)} />
          <LedgerRow label="Telnyx SMS add-on" value={fmt(calc.telnyxSmsAddon)} />
          <LedgerRow label="SMS usage (carrier + segment)" value={fmt(calc.smsUsage)} />
          <LedgerRow label={`Voice (${Math.round(calc.voiceCustomers)} of ${customers} customers)`} value={fmt(calc.voiceCostPerVoiceCustomer) + " avg"} muted />
          <LedgerRow label="Google Workspace (blended)" value={fmt(calc.workspaceCostPerCustomer)} />
          <LedgerRow label="Stripe processing" value={fmt(calc.stripeFee)} />
          <div style={{ display: "flex", justifyContent: "space-between", padding: "10px 0 6px", marginTop: 4, borderTop: "2px solid #1a1814" }}>
            <span style={{ fontFamily: "system-ui, sans-serif", fontSize: 13, fontWeight: 600, color: "#1a1814" }}>Total per customer</span>
            <span style={{ fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace", fontSize: 14, fontWeight: 700, color: "#1a1814", fontVariantNumeric: "tabular-nums" }}>
              {fmt(calc.totalCostPerCustomer)}
            </span>
          </div>
        </div>

        <div style={{ background: "#fff", borderRadius: 10, padding: "18px 18px 8px", border: "1px solid #eae6dd", marginTop: 14 }}>
          <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: "#9a8560", marginBottom: 10 }}>
            Totals at {customers} customers
          </div>
          <LedgerRow label="Total revenue" value={fmt0(calc.totalRevenue)} />
          <LedgerRow label="Total variable cost" value={fmt0(calc.totalCostPerCustomer * customers)} />
          <LedgerRow label="Fixed infra (VPS + 10DLC)" value={fmt(calc.totalFixedInfra)} />
          <div style={{ display: "flex", justifyContent: "space-between", padding: "10px 0 6px", marginTop: 4, borderTop: "2px solid #1a1814" }}>
            <span style={{ fontFamily: "system-ui, sans-serif", fontSize: 13, fontWeight: 600, color: "#1a1814" }}>Total margin</span>
            <span style={{ fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace", fontSize: 14, fontWeight: 700, color: marginColor, fontVariantNumeric: "tabular-nums" }}>
              {fmt0(calc.totalMargin)}
            </span>
          </div>
        </div>

        <div style={{ fontFamily: "system-ui, sans-serif", fontSize: 11, color: "#a39d8f", marginTop: 18, lineHeight: 1.6 }}>
          Estimates only — SMS/voice rates are blended averages, not exact billing. AI/LLM inference costs aren't included since both tiers are BYOK: the customer's own key bills to their own account, never yours.
        </div>
      </div>
    </div>
  );
}
