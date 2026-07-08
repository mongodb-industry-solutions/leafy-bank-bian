import styles from "./page.module.css";
import GlPipelineView from "@/components/GlMonitor/GlPipelineView";

export const metadata = {
  title: "Leafy Bank — GL Pipeline Monitor",
};

export default function GlMonitorPage() {
  return (
    <main className={styles.container}>
      <GlPipelineView />
    </main>
  );
}
