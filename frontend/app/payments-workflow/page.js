import styles from "./page.module.css";
import PaymentsWorkflowView from "@/components/PaymentsWorkflow/PaymentsWorkflowView";

export const metadata = {
  title: "Leafy Bank — Payments Workflow",
};

export default function PaymentsWorkflowPage() {
  return (
    <main className={styles.container}>
      <PaymentsWorkflowView />
    </main>
  );
}
