import styles from "./page.module.css";
import GlMonitor from "@/components/GlMonitor/GlMonitor";

export const metadata = {
  title: "Leafy Bank — GL Pipeline Monitor",
};

export default function GlMonitorPage() {
  return (
    <main className={styles.container}>
      <GlMonitor />
    </main>
  );
}
