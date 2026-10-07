import PaymentsHome from "@/components/PaymentsHome/PaymentsHome";

export const metadata = {
  title: "Leafy Bank — Payments",
};

export default function PaymentsPage() {
  return <PaymentsHome bianModelUrl={process.env.BIAN_MODEL_URL || "http://localhost:8004"} />;
}
