APPLYPILOT COMPACT KNOWLEDGE GRAPH
Optimized scorer input generated from the reviewed ApplyPilot knowledge graph.
{
  "schemaVersion": "applypilot_compact_knowledge_graph_v1",
  "generatedAt": "2026-06-11T00:07:33.711Z",
  "instructions": [
    "Use this compact graph as the factual evidence layer for ApplyPilot job-fit scoring.",
    "Treat canonicalFacts and termMeanings as evidence; cite their IDs when claiming fit, stretch transfer, or gaps.",
    "When a canonicalFact has sourceNodeIds, include relevant sourceNodeIds in evidenceNodeIds too; sourceNodeIds are stable across compact graph regenerations.",
    "Do not treat filteredTags as capabilities. They are workflow labels, generic label text, or redirected aliases.",
    "Use confirmedGaps to cap scores when a job requires missing credentials, direct domain background, tooling, or years.",
    "Use calibrationExamples only as human-label prior examples; never invent credentials or direct experience."
  ],
  "sourceSummary": {
    "resume": {
      "path": "C:\\Users\\JStal\\OneDrive\\Documents\\MasterResume\\StalloneJonathan7.docx",
      "contextPaths": [
        "C:\\Users\\JStal\\OneDrive\\Documents\\MasterResume\\Resume_Backup.docx"
      ],
      "extractedChars": 20001,
      "extractedLines": 99
    },
    "catalog": {
      "generatedAt": "2026-06-08T00:22:34.266Z",
      "items": 13260
    },
    "labels": {
      "events": 2021,
      "effectiveEvents": 1730,
      "scoringEvents": 1665,
      "fitMapFeedbackEntries": 2667,
      "dataQualitySkippedEvents": 74,
      "activeTriageEvents": 1120,
      "undoneTriageEvents": 2,
      "skippedTriageEvents": 9,
      "triageSuppressedByDetailed": 180
    },
    "seedProfileGraph": {
      "version": "applypilot_profile_evidence_graph_v1",
      "nodes": 5,
      "edges": 4
    },
    "profileEvidencePackets": {
      "count": 23
    },
    "termMeanings": {
      "count": 5
    },
    "transferEdges": {
      "seeded": 143,
      "approved": 18
    }
  },
  "labelScopeSummary": {
    "rawEvents": 2021,
    "effectiveEvents": 1730,
    "scoringEvents": 1665,
    "dataQualitySkippedEvents": 74,
    "meaning": "rawEvents are the full label log; effectiveEvents remove undone or suppressed workflow labels; scoringEvents exclude data-quality skips and are the safest calibration signal for scoring."
  },
  "labelEvidencePolicy": {
    "rules": [
      "Detailed labels override quick qualification triage for the same job.",
      "Quick qualification triage is a weak prior only when no detailed label exists.",
      "Qualification triage undo and triage skip events are audit-only, not scoring evidence.",
      "FitMap feedback is stronger than tag-only triage because it maps job requirements to human status labels."
    ],
    "confidenceTiers": {
      "detailed": "authoritative",
      "quickTriage": "weak_prior",
      "workflowEvents": "excluded_from_scoring"
    }
  },
  "counts": {
    "sourceResumeFacts": 86,
    "sourceProfileEvidencePackets": 23,
    "sourceCapabilities": 200,
    "sourceLabelObservations": 1730,
    "sourceFitMapObservations": 2667,
    "canonicalFacts": 78,
    "supportedCapabilities": 64,
    "confirmedGaps": 21,
    "filteredTags": 67,
    "termMeanings": 5,
    "aliasRedirects": 1
  },
  "labelOutcomeSummary": {
    "qualified": 305,
    "stretch": 164,
    "not_qualified": 1196
  },
  "filteredTagSummary": {
    "workflow": 2514,
    "gap": 1483,
    "label_text": 758,
    "alias_redirect": 0,
    "label_prior": 1191
  },
  "calibrationExamples": [
    {
      "id": "label:30f39e6b-6f5e-4324-b822-ec3a434e8e88",
      "title": "Analyst, Strategy & Operations",
      "company": "EV Realty",
      "bucket": "qualified",
      "rating": 8,
      "tags": [
        "operator-metrics",
        "resource-allocation",
        "multi-project-execution",
        "operator-ownership",
        "operator-financial-judgment",
        "missing-excel-financial-modeling",
        "operator-to-finance-strategy",
        "missing-consulting-background"
      ]
    },
    {
      "id": "label:cc785db8-eaa4-4f6e-b30a-08ec8b780aee",
      "title": "Business Development",
      "company": "HiringCafe",
      "bucket": "qualified",
      "rating": 8,
      "tags": [
        "operator-financial-judgment",
        "missing-finance-background",
        "missing-excel-financial-modeling",
        "operator-metrics",
        "resource-allocation"
      ],
      "reason": "I do not have an mba but am capable of everything they asked for in the job description"
    },
    {
      "id": "label:b795749e-45b4-4325-b5db-bfff2da15a17",
      "title": "Co-Founder & CEO - AI Communication Agents for Freight & Logistics",
      "company": "linkedin",
      "bucket": "qualified",
      "rating": 8,
      "tags": [
        "go-to-market",
        "gtm",
        "multi-project-execution",
        "operator-ownership",
        "ai-domain",
        "stretch-role"
      ],
      "reason": "I have founder experience that is very similar to what they are asking for"
    },
    {
      "id": "label:611a088e-986e-4079-a809-ac6fa9ecd22c",
      "title": "Analyst, Product Specialist - Digital Issuing",
      "company": "Mastercard",
      "bucket": "qualified",
      "rating": 10,
      "tags": []
    },
    {
      "id": "label:a2a12097-37bf-4137-8c2e-1d1238f3e403",
      "title": "Data Engineer, Smart Factory Solutions",
      "company": "Magna International",
      "bucket": "qualified",
      "rating": 10,
      "tags": []
    },
    {
      "id": "label:570f0981-2845-4237-aa6e-f757eb09ac3e",
      "title": "GTM Operations Senior Manager",
      "company": "Adobe",
      "bucket": "qualified",
      "rating": 8,
      "tags": [
        "operator-stakeholder-management",
        "cross-functional-leadership",
        "multi-project-execution",
        "operator-ownership",
        "missing-salesforce"
      ]
    }
  ],
  "supportedCapabilities": [
    {
      "tag": "operator-ownership",
      "label": "Operator Ownership",
      "support": {
        "resumeFacts": 7,
        "profilePackets": 0,
        "humanQualified": 95,
        "positiveLabels": 17
      },
      "caution": {
        "humanStretch": 7,
        "humanMissing": 27,
        "negativeLabels": 58
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "ai-domain",
      "label": "Ai Domain",
      "support": {
        "resumeFacts": 0,
        "profilePackets": 1,
        "humanQualified": 105,
        "positiveLabels": 18
      },
      "caution": {
        "humanStretch": 12,
        "humanMissing": 29,
        "negativeLabels": 70
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "multi-project-execution",
      "label": "Multi Project Execution",
      "support": {
        "resumeFacts": 4,
        "profilePackets": 0,
        "humanQualified": 95,
        "positiveLabels": 17
      },
      "caution": {
        "humanStretch": 7,
        "humanMissing": 27,
        "negativeLabels": 58
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "operator-metrics",
      "label": "Operator Metrics",
      "support": {
        "resumeFacts": 0,
        "profilePackets": 1,
        "humanQualified": 41,
        "positiveLabels": 7
      },
      "caution": {
        "humanStretch": 2,
        "humanMissing": 6,
        "negativeLabels": 30
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "resource-allocation",
      "label": "Resource Allocation",
      "support": {
        "resumeFacts": 1,
        "profilePackets": 0,
        "humanQualified": 41,
        "positiveLabels": 7
      },
      "caution": {
        "humanStretch": 2,
        "humanMissing": 6,
        "negativeLabels": 30
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "business-development",
      "label": "Business Development",
      "support": {
        "resumeFacts": 0,
        "profilePackets": 1,
        "humanQualified": 22,
        "positiveLabels": 12
      },
      "caution": {
        "humanStretch": 2,
        "humanMissing": 5,
        "negativeLabels": 11
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "structured-credit",
      "label": "Structured Credit",
      "support": {
        "resumeFacts": 10,
        "profilePackets": 2,
        "humanQualified": 0,
        "positiveLabels": 0
      },
      "caution": {
        "humanStretch": 0,
        "humanMissing": 0,
        "negativeLabels": 0
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "credit-analysis",
      "label": "Credit Analysis",
      "support": {
        "resumeFacts": 10,
        "profilePackets": 1,
        "humanQualified": 0,
        "positiveLabels": 0
      },
      "caution": {
        "humanStretch": 0,
        "humanMissing": 0,
        "negativeLabels": 0
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "structured-finance",
      "label": "Structured Finance",
      "support": {
        "resumeFacts": 11,
        "profilePackets": 0,
        "humanQualified": 0,
        "positiveLabels": 0
      },
      "caution": {
        "humanStretch": 0,
        "humanMissing": 0,
        "negativeLabels": 0
      },
      "evidenceIds": [],
      "examples": []
    },
    {
      "tag": "capital-markets",
      "label": "Capital Markets",
      "support": {
        "resumeFacts": 10,
        "profilePackets": 0,
        "humanQualified": 0,
        "positiveLabels": 0
      },
      "caution": {
        "humanStretch": 0,
        "humanMissing": 0,
        "negativeLabels": 0
      },
      "evidenceIds": [],
      "examples": []
    }
  ],
  "confirmedGaps": [
    {
      "tag": "missing-years",
      "label": "Missing Years",
      "missingCount": 53,
      "stretchCount": 8,
      "negativeLabelCount": 69,
      "evidenceIds": [
        "label:2a61b01c-32d1-4518-8d64-5003dfb4e57f",
        "label:1917518a-9cee-4d4e-a9cf-6b2f7f62d98c"
      ],
      "examples": [
        "1/10 downvote: I do not have 7+ years of developing, negotiating and executing business agreements experience or 7+ y..."
      ]
    },
    {
      "tag": "missing-consulting-background",
      "label": "Missing Consulting Background",
      "missingCount": 33,
      "stretchCount": 9,
      "negativeLabelCount": 46,
      "evidenceIds": [
        "seed_profile_graph:profile-evidence:formal-finance-background-not-established:1d8vo5f",
        "absence_check:constraint:management-consulting-background-not-established:1787a8k"
      ],
      "examples": [
        "Formal finance background not established: Current profile evidence does not establish investment banking, consulting..."
      ]
    },
    {
      "tag": "missing-clinical-background",
      "label": "Missing Clinical Background",
      "missingCount": 26,
      "stretchCount": 1,
      "negativeLabelCount": 22,
      "evidenceIds": [
        "label:ede972a1-959e-489a-8b08-2c4636f99824",
        "label:d2eaba4f-28e1-445a-917e-1e2e8616864f"
      ],
      "examples": [
        "1/10 downvote: I am not in the field of healthcare as in providing physical care to patients"
      ]
    },
    {
      "tag": "missing-license-or-certification",
      "label": "Missing License Or Certification",
      "missingCount": 25,
      "stretchCount": 0,
      "negativeLabelCount": 24,
      "evidenceIds": [
        "label:20fbdeea-f9ff-4e25-8186-05913c7263ef",
        "label:7abc87f7-14f9-4ea8-95cb-ff2475f85d08"
      ],
      "examples": [
        "1/10 downvote: not an accountant"
      ]
    },
    {
      "tag": "missing-excel-financial-modeling",
      "label": "Missing Excel Financial Modeling",
      "missingCount": 11,
      "stretchCount": 7,
      "negativeLabelCount": 41,
      "evidenceIds": [
        "seed_profile_graph:profile-evidence:formal-finance-background-not-established:1d8vo5f",
        "label:2c76e070-e8d2-48be-8dc4-bf2a5a26fe4a"
      ],
      "examples": [
        "Formal finance background not established: Current profile evidence does not establish investment banking, consulting..."
      ]
    }
  ],
  "canonicalFacts": [
    {
      "id": "cfact:072",
      "label": "Has built ApplyPilot as an AI-assisted job-fit, knowledge graph, calibration, and review tool.",
      "evidence": "User states that he has built plenty of side projects, including ApplyPilot, an AI-assisted tool that uses job descriptions, human labels, fit-map feedback, resume context, and a knowledge graph to reason about job fit.",
      "confidence": "medium",
      "tags": [
        "ai-tool-building",
        "ai-tools",
        "automation",
        "business-technology",
        "data-analysis",
        "linux",
        "llm-tools",
        "private-technical-capability",
        "proprietary-tool-building",
        "scripting",
        "side-project-software",
        "software-product-building",
        "technical-fluency",
        "technical-project-execution",
        "technology-problem-solving"
      ],
      "sources": [
        "profile_packet"
      ],
      "sourceNodeIds": [
        "technology:applypilot-ai-tool-building"
      ]
    },
    {
      "id": "cfact:010",
      "label": "My role as a senior analyst on the structured credit team covered a few thing...",
      "evidence": "My role as a senior analyst on the structured credit team covered a few things but mainly the group's job was to rate new CLO's and to surveil existing ones. We would occasionally do some sort of specialized product such as Trust preferred securities or pri...",
      "confidence": "medium",
      "tags": [
        "capital-markets",
        "clos",
        "credit-analysis",
        "credit-markets",
        "financial-services",
        "structured-credit",
        "structured-finance",
        "trust-preferred-securities"
      ],
      "sources": [
        "resume_context"
      ],
      "sourceNodeIds": [
        "resume_context:experience:my-role-as-a-senior-analyst-on-the-structured-credit-team-covered-a-few-thing:y8ifyu",
        "resume_context:experience:my-role-as-a-senior-analyst-on-the-structured-credit-team-covered-a-few-thing:62elp3",
        "resume_context:experience:we-would-occasionally-do-some-sort-of-specialized-product-such-as-trust-prefe:ihzp0a"
      ]
    },
    {
      "id": "cfact:032",
      "label": "Kroll Bond Ratings Agency, Senior Analyst, Structured Credit (Remote) March 2...",
      "evidence": "Kroll Bond Ratings Agency, Senior Analyst, Structured Credit (Remote) March 2020 March 2022",
      "confidence": "medium",
      "tags": [
        "capital-markets",
        "credit-analysis",
        "credit-markets",
        "credit-rating-agency",
        "financial-services",
        "structured-credit",
        "structured-finance"
      ],
      "sources": [
        "resume"
      ],
      "sourceNodeIds": [
        "resume:experience:kroll-bond-ratings-agency--senior-analyst--structured-credit--remote--march-2:1t1jxor",
        "resume:skill:credit-rating-agency:vl4vfl",
        "resume:skill:structured-credit:173050y"
      ]
    },
    {
      "id": "cfact:033",
      "label": "Surveillance lead on entire $2bn+ Trust Preferred Security book ; assisted US...",
      "evidence": "Surveillance lead on entire $2bn+ Trust Preferred Security book ; assisted US/Euro new issuance reports $5bn+.",
      "confidence": "medium",
      "tags": [
        "capital-markets",
        "credit-analysis",
        "credit-markets",
        "financial-services",
        "structured-credit",
        "structured-finance",
        "trust-preferred-securities"
      ],
      "sources": [
        "resume"
      ],
      "sourceNodeIds": [
        "resume:experience:surveillance-lead-on-entire--2bn--trust-preferred-security-book---assisted-us:1m2m2p4",
        "resume:skill:credit-analysis:17thsq9",
        "resume:skill:trust-preferred-securities:19jhg45"
      ]
    },
    {
      "id": "cfact:073",
      "label": "Has private hands-on technical depth that is intentionally not fully described on the public resume.",
      "evidence": "User states that he has scripting experience, has used Linux for many years, has built proprietary technical tools whose details should remain confidential/redacted, and has substantial hands-on programming experience beyond what is disclosed on the one-pag...",
      "confidence": "medium",
      "tags": [
        "automation",
        "business-technology",
        "linux",
        "private-technical-capability",
        "proprietary-tool-building",
        "scripting",
        "technical-fluency",
        "technical-learning-capacity",
        "technical-project-execution",
        "technology-problem-solving"
      ],
      "sources": [
        "profile_packet"
      ],
      "sourceNodeIds": [
        "technology:private-technical-capability-redacted"
      ]
    },
    {
      "id": "cfact:058",
      "label": "Professional structured-credit rating and surveillance work should count as finance and credit-analysis experience.",
      "evidence": "Resume and resume context describe Senior Analyst, Structured Credit work rating and surveilling CLOs and trust preferred securities; user wrote during threshold review: \"Working in structured credit is finance experience.\"",
      "confidence": "high",
      "tags": [
        "credit-analysis",
        "finance-background",
        "financial-analysis",
        "portfolio-analysis",
        "risk-analysis",
        "structured-credit",
        "technical-finance"
      ],
      "sources": [
        "profile_packet"
      ],
      "sourceNodeIds": [
        "finance:structured-credit-professional-finance-context"
      ]
    },
    {
      "id": "cfact:060",
      "label": "Has 3 years of recurring relationship development and outbound phone outreach in the GGG Construction role.",
      "evidence": "User states that in the current GGG Construction role he spends significant time on the phone each day or week maintaining existing relationships and developing new relationships, including outreach to people he has not met before.",
      "confidence": "medium",
      "tags": [
        "business-development",
        "cold-outreach",
        "commercial-communication",
        "outbound-calling",
        "prospecting",
        "relationship-management",
        "sales-adjacent-experience",
        "stakeholder-development",
        "vendor-relationship-management"
      ],
      "sources": [
        "profile_packet"
      ],
      "sourceNodeIds": [
        "sales:ggg-relationship-development-cold-outreach"
      ]
    },
    {
      "id": "cfact:062",
      "label": "Cold-called approximately 420 college coaches over 3 months while helping build a software and service recruiting business.",
      "evidence": "User states that during a summer in college he helped a friend build a software and service recruiting business and cold-called 420 college coaches over 3 months.",
      "confidence": "medium",
      "tags": [
        "cold-calling",
        "commercial-communication",
        "high-volume-outreach",
        "outbound-sales",
        "prospecting",
        "recruiting-business-development",
        "relationship-initiation",
        "sales-adjacent-experience"
      ],
      "sources": [
        "profile_packet"
      ],
      "sourceNodeIds": [
        "sales:college-recruiting-business-cold-calling"
      ]
    }
  ],
  "aliasRedirects": [
    {
      "fromTag": "public-work-bidding",
      "toTag": "construction-bid-estimating",
      "reason": "term alias"
    }
  ],
  "termMeanings": [
    {
      "id": "term:capital-markets-credit-rating-agency",
      "canonicalTag": "capital-markets",
      "label": "Capital markets / credit rating agency domain bridge",
      "aliases": [
        "KBRA",
        "Kroll Bond Rating Agency",
        "Kroll Bond Ratings Agency",
        "credit rating agency",
        "ratings agency",
        "NRSRO",
        "Nationally Recognized Statistical Rating Organization",
        "Wall Street structured credit",
        "structured finance",
        "credit markets",
        "fixed income",
        "debt capital markets"
      ],
      "definition": "KBRA / Kroll Bond Rating Agency is an SEC-recognized credit rating agency / NRSRO. Senior Analyst structured-credit work rating and surveilling CLOs, Trust Preferred Securities, and private ratings is direct evidence for capital markets, credit markets, structured finance, financial services, credit analysis, and ra...",
      "appliesWhen": [
        "A job asks for broad capital markets, credit markets, structured finance, fixed income, securitized products, ratings agency, NRSRO, CLO, credit an...",
        "A requirement asks for experience analyzing credit products, rating securities, surveilling portfolios, reviewing structured-credit assets, or work..."
      ],
      "notSameAs": [
        "investment banking execution",
        "trading desk experience",
        "equity derivatives trading",
        "capital-markets pricing model validation"
      ],
      "evidenceIds": [
        "resume_context:experience:my-role-as-a-senior-analyst-on-the-structured-credit-team-covered-a-few-thing:y8ifyu",
        "resume_context:experience:my-role-as-a-senior-analyst-on-the-structured-credit-team-covered-a-few-thing:62elp3"
      ]
    },
    {
      "id": "term:chief-operating-officer-executive-operations",
      "canonicalTag": "chief-operating-officer",
      "label": "Chief Operating Officer / executive operations",
      "aliases": [
        "COO",
        "Chief Operating Officer",
        "executive operations",
        "company operations",
        "operator ownership",
        "business operations leadership"
      ],
      "definition": "Executive operating responsibility at GGG Construction, where COO/Project Manager means company-level operating ownership supported by prime-contractor responsibility, project portfolio execution, bid/estimating ownership, vendor/subcontractor coordination, and resource allocation.",
      "appliesWhen": [
        "A job asks for COO, Chief Operating Officer, business operations, company operations, operator, Chief of Staff, founder's office, or ownership-heav...",
        "A requirement is about owning operations across projects, vendors, resources, bids, and execution rather than only performing task-level project ma..."
      ],
      "notSameAs": [
        "CFO",
        "CTO",
        "CMO",
        "public-company executive"
      ],
      "evidenceIds": [
        "resume_context:experience:i-have-many-roles-there--i-m-the-project-manager-for-all-of-our-jobs-that-we:11oby3o",
        "resume_context:experience:i-m-the-project-manager-for-all-of-our-jobs-that-we-are-the-prime-contractor:tzxmy5"
      ]
    },
    {
      "id": "term:construction-bid-estimating",
      "canonicalTag": "construction-bid-estimating",
      "label": "Construction bid estimating",
      "aliases": [
        "estimating",
        "bid estimating",
        "construction estimating",
        "public-work bidding",
        "preconstruction estimating",
        "cost estimating",
        "bid package preparation",
        "scope review",
        "plans and specifications review"
      ],
      "definition": "Estimating and preparing construction/public-work bids by reading plans and specifications, understanding scope, estimating labor/material/equipment approach and cost, using historical cost data, preparing bid paperwork, and submitting bids.",
      "appliesWhen": [
        "A job asks for construction estimating, bid estimating, bid packages, preconstruction estimating, public-work bids, scope review, or plans/specific...",
        "A requirement is about pricing work, estimating costs, reading project plans/specifications, or preparing construction bid documents."
      ],
      "notSameAs": [
        "generic sales proposals",
        "software RFP writing",
        "fundraising pitch decks",
        "grant writing"
      ],
      "evidenceIds": [
        "resume_context:skill:construction-bid-estimating:mhm26t",
        "resume_context:experience:when-i-then-understand-the-scope-of-work-plans-and-specifications--i-can-now:1p5uvss"
      ]
    },
    {
      "id": "term:private-technical-builder-context",
      "canonicalTag": "private-technical-capability",
      "label": "Private technical builder context",
      "aliases": [
        "private technical capability",
        "scripting",
        "Linux",
        "proprietary internal tools",
        "proprietary technical tools",
        "neural network projects",
        "hardcore programming"
      ],
      "definition": "Private/redacted technical work, scripting, Linux exposure, proprietary internal tool-building, and neural-network side projects can support direct technical-fluency or stretch-transfer requirements when the job explicitly asks for those capabilities. Redacted proprietary claims should remain high-level and should n...",
      "appliesWhen": [
        "A requirement directly asks for scripting, Linux, proprietary/internal tool building, neural-network projects, machine learning exposure, or hands-...",
        "A role is technical-adjacent and the requirement can be satisfied by transferable hands-on technical project evidence rather than formal production..."
      ],
      "notSameAs": [
        "production software engineering",
        "machine learning engineering",
        "MLOps engineer",
        "distributed systems engineering"
      ],
      "evidenceIds": [
        "resume_context:skill:neural-networks:1g5sae8",
        "resume:skill:machine-learning:svo5rw"
      ]
    },
    {
      "id": "term:business-technology-ai-tool-building",
      "canonicalTag": "technology-problem-solving",
      "label": "Business technology / AI tool building",
      "aliases": [
        "AI tool building",
        "LLM tools",
        "automation",
        "business technology",
        "technical project execution",
        "technology to solve business problems",
        "AI-assisted workflow",
        "knowledge graph tool",
        "side-project software",
        "business process automation"
      ],
      "definition": "Hands-on use of AI/LLM tools, side-project software building, ApplyPilot, automation, and technology-backed workflow design are evidence of using technology to solve business problems and executing technical projects from an operator/business context.",
      "appliesWhen": [
        "A job asks for using technology to solve business problems, automation, AI tools, LLM tools, BI, technical project execution, business systems, or...",
        "A requirement is about translating business needs into tools, workflows, experiments, automation, or AI-enabled operating processes."
      ],
      "notSameAs": [
        "production software engineering",
        "machine learning engineering",
        "data scientist",
        "MLOps engineer"
      ],
      "evidenceIds": [
        "resume:skill:llm-tools:5wbgv5"
      ]
    }
  ]
}
