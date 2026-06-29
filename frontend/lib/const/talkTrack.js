// talkTrack.js
export const TALK_TRACK = [
  {
    heading: "How to Demo",
    content: [
      {
        heading: "What is Interactive Banking?",
        body: "Interactive banking uses generative AI, like chatbots and virtual assistants, to provide real-time, personalized, and seamless customer experiences. It enhances self-service by resolving queries instantly, offering tailored advice, and keeping interactions within banking apps, making banking smarter, more efficient, and user-friendly.",
      },
      {
        heading: "How to Demo",
        body: [
          "Select one of the Suggested Questions or type a new one in the prompt",
          'Click "Ask"',
          "View response",
        ],
      },
    ],
  },
  {
    heading: "Behind the Scenes",
    content: [
      {
        heading: "Data Flow",
        body: "",
      },
      {
        image: {
          src: "/chatbot_diagram.png",
          alt: "Architecture",
        },
      },
    ],
  },
  {
    heading: "Why MongoDB",
    content: [
      {
        heading: "Flexibility",
        body: "MongoDB's flexible document model unifies structured and unstructured data, creating a consistent dataset that enhances the AI's ability to understand and respond to complex queries. This model enables financial institutions to store and manage customer data, transaction history, and document content within a single system, streamlining interactions and making AI responses more contextually relevant.",
      },
      {
        heading: "Atlas Vector Search",
        body: "MongoDB Atlas Vector Search makes it easy to perform semantic searches on vectorized document chunks, quickly retrieving the most relevant information to answer user questions. This capability allows the AI to find precise answers within dense documents, enhancing the self-service experience for customers.",
      },
      {
        heading: "Integration with AI Models",
        body: "MongoDB integrates smoothly with major AI frameworks, facilitating rapid and efficient deployment of scalable AI applications in banks. Aligning MongoDB with cloud-based LLMs ensures accurate and real-time responses to customer queries using top tools, meeting demand effectively.",
      },
    ],
  },
];