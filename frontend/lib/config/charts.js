// Atlas Charts embed IDs, per environment.
//
// Staging and prod use the same Charts project (baseUrl lives in
// ConsentGatedChart), but each environment's charts read from that
// environment's own cluster, so the chart IDs differ. The active set is
// selected at build time via NEXT_PUBLIC_CHARTS_ENV (set per-stage in the
// drone build_args); defaults to staging for local docker-compose.
const CHART_IDS = {
  staging: {
    fridaklo: {
      top: "8867e720-081f-4b5a-9302-fb9b2b3622db", // pie
      lower: "fdc4b222-d67f-44d1-8809-767eae9e4f8a", // bar
    },
    gracehop: {
      top: "c5fc1948-d42d-4e46-a3c2-3e0c3cb1e637", // pie
      lower: "62d1db18-3a11-4806-b5b6-3fbdd5482f45", // bar
    },
  },
  prod: {
    fridaklo: {
      top: "e73a1d94-1843-40c9-a9f6-343fb8e4ce10", // pie
      lower: "a60d07ad-a60d-4ace-9acd-8f0a3ec34e02", // bar
    },
    gracehop: {
      top: "fd79edf5-daf7-4368-84e5-ee61e5f68324", // pie
      lower: "6cf6306d-d1d2-4a7a-82d1-b130765431ae", // bar
    },
  },
};

const env = process.env.NEXT_PUBLIC_CHARTS_ENV ?? "staging";

if (!CHART_IDS[env]) {
  throw new Error(
    `Invalid NEXT_PUBLIC_CHARTS_ENV: "${env}". Expected "staging" or "prod".`
  );
}

export const chartIds = CHART_IDS[env];
