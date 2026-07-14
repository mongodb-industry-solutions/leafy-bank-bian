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
          "Two retail customers are available for the Open Finance journey:",
          "",
          "**Frida (Spender profile)**:",
          "- Existing accounts, transactions, and financial history at Leafy Bank.",
          "- Green Bank → Payroll Deductible Loan (11.99%, $24,150 balance)",
          "- MongoDB Bank → Vehicle Loan (11.99%, $10,100) + Personal Loan (12.99%, $7,150)",
          "- *Transaction history:* Green Bank and MongoDB Bank. Connect either one for the financial-advice path.",
          "",
          "**Grace (Saver profile)**:",
          "- Existing Leafy Bank customer with a conservative spending pattern.",
          "- NeoFinance → Personal Loan (6.50%, $840 balance)",
          "- *Transaction history:* NeoFinance, Green Bank, and MongoDB Bank."
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
          "- Review the permissions the agent will access and why. You can respond three ways:",
          "  - **I accept** — grant the full default permission set",
          "  - **Remove permissions** — drop any data category you're not comfortable sharing before accepting",
          "  - **I decline** — cancel the request",
          "- The consent duration is fixed at **30 days** — the agent states it and asks for acceptance.",
          "- 'Log in' via the new tab and approve the consent request.",
          "",
          "After approving, the tab shows 'Consent approved!' with a **'You can close this tab'** confirmation."
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
          "- **Agentic data access with the MongoDB MCP Server**: Expose MongoDB collections as tools that LLM agents invoke directly. The Financial Advice Agent answers natural language queries autonomously, no custom tool code required.",
          "- **Supervisor agent orchestration**: LangGraph orchestrates two specialized agents — a Consent Agent (external bank consent lifecycle) and a Financial Advice Agent (spending and portability analysis via the MongoDB MCP Server) — and persists conversation state through checkpoint collections in MongoDB Atlas.",
          "- **Context-aware suggestion engine**: A Claude Haiku model generates the follow-up suggestion chips shown after each reply, keyed to the current step of the consent and advice flow."
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
