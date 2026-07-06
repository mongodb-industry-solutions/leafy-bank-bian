// openFinanceTalkTrack.js
export const OPEN_FINANCE_TALK_TRACK = [
  {
    heading: "How to Demo",
    content: [
      {
        body: [
          "Understand how this solution demonstrates a next-generation Open Finance experience using MongoDB Atlas and agentic AI.",
          "See how a multi-agent system orchestrates consent, retrieves external financial data, and generates loan portability insights in real time.",
          "Explore how MongoDB powers the entire flow — from secure consent storage with Queryable Encryption to transaction classification with Vector Search and real-time analytics with aggregation pipelines."
        ]
      },
      {
        heading: "1. Select a User Profile",
        body: [
          "**Frida (banked user)**:",
          "- Existing accounts, transactions, and financial history at Leafy Bank.",
          "- Green Bank → Payroll Deductible Loan (3.67%, $20,000)",
          "- MongoDB Bank → Vehicle Loan (4.63%, $10,000) + Personal Loan (5.25%, $1,500)",
          "",
          "**Helly (unbanked user)**:",
          "- No prior relationship with Leafy Bank.",
          "- NeoFinance → Personal Loan (6.50%, $1,200)"
        ]
      },
      {
        heading: "2. Enable Pop-ups",
        body: "Ensure pop-ups are enabled in your browser. The demo opens external bank authentication and consent approval flows in a new tab. Blocking pop-ups will interrupt the experience."
      },
      {
        heading: "3. Start the Journey",
        body: [
          "Click one of the two CTAs (the large buttons with GIFs). Each triggers a predefined journey:",
          "- **Loan Portability** — Connect an external bank, analyze your loan, and receive a portability offer.",
          "- **Financial Advice** — Connect an external bank and get a spending health analysis.",
          "",
          "The assistant begins the flow automatically. Follow the conversation as the supervisor agent routes your request to the appropriate specialist agent."
        ]
      },
      {
        heading: "4. Complete the Consent Flow",
        body: [
          "When prompted:",
          "- Select an external bank",
          "- 'Log in' via the new tab",
          "- Approve the consent request",
          "",
          "You will automatically return to the demo to continue."
        ]
      },
      {
        heading: "5. Explore the Agent Reasoning",
        body: [
          "Watch how the agent:",
          "- Retrieves internal and external data",
          "- Classifies transactions using Vector Search",
          "- Computes financial metrics using aggregation pipelines",
          "- Generates a loan portability recommendation",
          "",
          "Click on MongoDB feature labels in the UI to expand each step and inspect how the data is processed."
        ]
      },
      {
        heading: "6. Review the Outcome",
        body: [
          "Observe the final response:",
          "- Loan portability offer and potential savings",
          "- Supporting financial analysis",
          "- Clear explanation of how the decision was made",
          "",
          "Ask follow-up questions to continue the conversation — the agent maintains context using MongoDB-backed memory."
        ]
      }
    ]
  },
  {
    heading: "Behind the Scenes",
    content: [
      {
        body: [
          "**MongoDB Vector Search, MongoDB MCP Server, MongoDB Queryable Encryption** form the backbone of this solution.",
          "**MongoDB Atlas** serves as the operational data layer that underpins these open finance architectures."
        ]
      },
      {
        heading: "Core Capabilities",
        body: [
          "- **Queryable Encryption for consent privacy**: Protect consumer identity across every consent lifecycle event — creation, authorization, data retrieval, and revocation. The server never sees plaintext. You query encrypted fields with standard equality filters without changing application code.",
          "- **Agentic data access with the MongoDB MCP Server**: Expose MongoDB collections as tools that LLM agents invoke directly. The internal data agent can answer natural language queries autonomously, no custom tool code required.",
          "- **Supervisor agent orchestration**: LangGraph orchestrates specialized agents — Consent Agent, Portability Agent, and Internal Data Agent — and persists conversation state through checkpoint collections in MongoDB Atlas."
        ]
      },
      {
        image: {
          src: "/behind-the-scenes.png",
          alt: "Architecture overview"
        }
      }
    ]
  },
  {
    heading: "Why MongoDB",
    content: [
      {
        body: [
          "MongoDB Atlas key capabilities in this solution:",
          "- **Persisting LangGraph conversation state**: through checkpoint collections.",
          "- **Running aggregation pipelines**: compute balances, debt totals, portability savings, and spending scores across internal and external data.",
          "- **Separating data in a dual-database architecture**: internal (`leafy_bank`) and external (`open_finance`) aligned with Open Finance and ISO 20022-style transaction models.",
          "- **Unify open finance data on MongoDB Atlas**: Reduce integration complexity by consolidating internal and external datasets.",
          "- **Simplify analytics with aggregation pipelines**: Compute balances, debt totals, portability savings, and spending scores in a single query path.",
          "- **Protect sensitive consent data with queryable encryption**: Query sensitive fields while maintaining strong privacy controls.",
          "- **Streamline consent journeys with agentic AI**: Use LangGraph-based multi-agent chatbots to reduce abandonment and improve customer experience.",
          "- **Align data structures with ISO 20022 best practices**: Standardize external transaction fields and codes across institutions."
        ]
      }
    ]
  }
];