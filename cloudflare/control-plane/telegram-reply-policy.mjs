// Source messages are evidence, never instructions. Only the authenticated
// operator supplies editorial decisions and exact outbound text.
import { ControlError } from "./kernel.mjs";

export const REVIEW_STAGES = [
  "research_review",
  "opportunity_review",
  "reply_draft",
  "reply_review",
];
const deny = (reason) => {
  throw new ControlError(reason, 409);
};
export function validatePreparedReply(input) {
  if (
    !/^@[a-zA-Z][a-zA-Z0-9_]{3,31}$/.test(input.target || "") ||
    !Number.isSafeInteger(input.question_message_id) ||
    input.question_message_id < 1 ||
    typeof input.text !== "string" ||
    !["en", "ru"].includes(input.review?.language)
  )
    deny("InvalidPreparedReply");
  const text = input.text.trim();
  const words = text.match(/[\p{L}\p{N}]+(?:['’_-][\p{L}\p{N}]+)*/gu) || [];
  if (
    words.length < 30 ||
    words.length > 90 ||
    text.split(/\n\s*\n/).length > 2 ||
    (text.match(/[?？]/g) || []).length > 1 ||
    text.length > 3000
  )
    deny("PreparedReplyLengthOrFormat");
  if (
    input.review.language === "ru"
      ? !/[А-Яа-яЁё]/.test(text)
      : /[А-Яа-яЁё]/.test(text)
  )
    deny("PreparedReplyLanguageMismatch");
  for (const key of [
    "topic_allowed",
    "free_real_identity",
    "automation_allowed",
    "rules_allow_replies",
    "question_unresolved",
    "claims_verified",
    "language_matches",
    "support_only",
    "requires_ai_disclosure",
  ])
    if (typeof input.review[key] !== "boolean")
      deny("PreparedReviewDecisionRequired");
  if (!input.review.citations || typeof input.review.citations !== "object")
    deny("PreparedReviewCitationsRequired");
  const allowed = [
    "language",
    "topic_allowed",
    "free_real_identity",
    "automation_allowed",
    "rules_allow_replies",
    "question_unresolved",
    "claims_verified",
    "language_matches",
    "support_only",
    "requires_ai_disclosure",
    "ai_disclosure_included",
    "discussion_continuing",
    "relationship_disclosed",
    "recommendation_allowed",
    "links_allowed",
  ];
  const review = Object.fromEntries(
    allowed
      .filter((key) => Object.hasOwn(input.review, key))
      .map((key) => [key, input.review[key]]),
  );
  for (const key of allowed.filter((key) => key !== "language"))
    if (Object.hasOwn(review, key) && typeof review[key] !== "boolean")
      deny("PreparedReviewDecisionRequired");
  review.citations = {};
  for (const key of allowed.filter((key) => key !== "language")) {
    const list = input.review.citations[key];
    if (list !== undefined) {
      if (
        !Array.isArray(list) ||
        list.length > 10 ||
        list.some(
          (c) => typeof c?.source !== "string" || typeof c?.quote !== "string",
        )
      )
        deny("PreparedReviewCitationsRequired");
      review.citations[key] = list.map((c) => ({
        source: c.source,
        quote: c.quote,
      }));
    }
  }
  if (JSON.stringify(review).length > 32000) deny("PreparedReviewTooLarge");
  return {
    target: input.target.toLowerCase(),
    question_message_id: input.question_message_id,
    text,
    review,
  };
}
export function sourceMap(observation, context) {
  const sources = { about: observation.about || "" };
  for (const m of observation.pinned || [])
    sources["pin:" + m.id] = m.text || "";
  for (const m of observation.recent || [])
    sources["recent:" + m.id] = m.text || "";
  if (context?.source) sources["context:source"] = context.source.text || "";
  for (const m of context?.replies || [])
    sources["context:reply:" + m.id] = m.text || "";
  return sources;
}
function cite(packet, key, sources) {
  const citations = packet.review.citations[key];
  if (
    !Array.isArray(citations) ||
    !citations.length ||
    citations.length > 10 ||
    citations.some(
      (c) =>
        typeof c.source !== "string" ||
        typeof c.quote !== "string" ||
        !c.quote.trim() ||
        c.quote.length > 2000 ||
        !sources[c.source]?.includes(c.quote),
    )
  )
    deny("PreparedReviewCitationMismatch");
  return citations.map((c) => ({ ...c, decision: key }));
}
export function verifyObservation(result, packet, account, now) {
  const evidence = result?.evidence?.find(
    (e) => e.transport === "authenticated_mtproto" && e.account_id === account,
  );
  const o = evidence?.observation;
  if (
    result?.verdict !== "pass" ||
    !o?.identity_checked ||
    !o.is_group ||
    o.target?.toLowerCase() !== packet.target ||
    !/^\d+$/.test(o.discussion_id || "") ||
    result.facts?.rules_read !== true ||
    o.pinned_truncated ||
    o.forum_topics_unchecked ||
    o.storage_sample_truncated ||
    !Number.isFinite(Date.parse(o.checked_at)) ||
    now - Date.parse(o.checked_at) / 1000 > 86400 ||
    Date.parse(o.checked_at) / 1000 > now + 60
  )
    deny("AuthenticatedObservationRequired");
  const rules = [o.about, ...(o.pinned || []).map((m) => m.text)].join("\n");
  if (
    (/https?:\/\//i.test(rules) && /rules|правил/i.test(rules)) ||
    /[!/]rules\b/i.test(rules)
  )
    deny("CompleteRulesRequired");
  return o;
}
export function researchReview(observation, packet, now) {
  const r = packet.review,
    sources = sourceMap(observation);
  const recent = (observation.recent || []).filter(
    (m) =>
      m.author_is_bot === false &&
      m.author_hash &&
      Number.isFinite(Date.parse(m.date)) &&
      Date.parse(m.date) / 1000 >= now - 604800 &&
      Date.parse(m.date) / 1000 <= now + 60,
  );
  if (
    new Set(recent.map((m) => m.author_hash)).size < 2 ||
    !recent.some((m) => m.reply_to)
  )
    deny("RecentHumanDiscussionRequired");
  for (const key of [
    "topic_allowed",
    "free_real_identity",
    "automation_allowed",
    "rules_allow_replies",
  ])
    if (r[key] !== true) deny("PreparedResearchNotQualified");
  const rules = [
    observation.about,
    ...(observation.pinned || []).map((m) => m.text),
  ].join("\n");
  if (
    /\bno\s+(?:userbots?|bots?|automated\s+(?:posts?|replies|comments)|ai[- ]generated\s+(?:posts?|replies|comments))\b|(?:userbots?|ai[- ]generated|automated\s+(?:posts?|replies|comments)).{0,30}(?:prohibited|forbidden|not allowed)|запрещ.{0,40}(?:юзербот|автоматическ|комментар.{0,12}ии)/is.test(
      rules,
    )
  )
    deny("AutomationForbiddenByRules");
  return {
    verdict: "pass",
    language: r.language,
    support_only: r.support_only,
    facts: {
      identity_verified: true,
      discussion_entry: true,
      topic_allowed: true,
      free_real_identity: true,
      recent_human_discussion: true,
      rules_read: true,
      deduplicated_discussion: true,
      automation_allowed: true,
    },
    discussion_id: observation.discussion_id,
    evidence: [
      "topic_allowed",
      "free_real_identity",
      "automation_allowed",
      "rules_allow_replies",
    ].flatMap((key) => cite(packet, key, sources)),
  };
}
export function verifyContext(
  context,
  observation,
  packet,
  now,
  previousQuestion = null,
) {
  const s = context?.source;
  if (
    !s ||
    context.sample_truncated ||
    context.target?.toLowerCase() !== packet.target ||
    context.discussion_id !== observation.discussion_id ||
    s.id !== packet.question_message_id ||
    s.out !== false ||
    s.author_is_bot !== false ||
    !s.author_hash ||
    !s.text ||
    !Number.isFinite(Date.parse(s.date)) ||
    Date.parse(s.date) / 1000 > now + 60 ||
    !Number.isFinite(Date.parse(context.checked_at)) ||
    now - Date.parse(context.checked_at) / 1000 > 120 ||
    Date.parse(context.checked_at) / 1000 > now + 60
  )
    deny("CompleteFreshReplyContextRequired");
  if (
    previousQuestion &&
    (s.text !== previousQuestion.text ||
      s.date !== previousQuestion.published_at)
  )
    deny("PreparedQuestionChanged");
  const age = now - Date.parse(s.date) / 1000;
  if (
    age > 86400 &&
    packet.review.question_unresolved !== true &&
    packet.review.discussion_continuing !== true
  )
    deny("QuestionNeedsContinuingDiscussion");
  if (
    age > 259200 &&
    (packet.review.discussion_continuing !== true ||
      !(context.replies || []).some(
        (m) => Date.parse(m.date) / 1000 >= now - 259200,
      ))
  )
    deny("QuestionTooOld");
  return {
    message_id: s.id,
    discussion_id: context.discussion_id,
    text: s.text,
    published_at: s.date,
    author_hash: s.author_hash,
  };
}
export function opportunityReview(context, observation, packet, now) {
  const question = verifyContext(context, observation, packet, now);
  if (packet.review.question_unresolved !== true)
    deny("QuestionAlreadyResolved");
  const sources = sourceMap(observation, context);
  return {
    verdict: "pass",
    question,
    context,
    facts: {
      automation_allowed: true,
      question_unresolved: true,
      context_read: true,
      reply_target_verified: true,
      discussion_continuing: packet.review.discussion_continuing === true,
    },
    evidence: cite(packet, "question_unresolved", sources),
    context_checked_at: context.checked_at,
  };
}
export function exactDraft(packet) {
  return {
    verdict: "pass",
    text: packet.text,
    language: packet.review.language,
    evidence: [
      { source: "operator_prepared_exact_text", packet_id: packet.id },
    ],
  };
}
function textFingerprint(text) {
  return text
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}
export function similarReply(left, right) {
  const a = textFingerprint(left),
    b = textFingerprint(right);
  if (a === b) return true;
  const grams = (value) => {
    const words = value.split(" ");
    return new Set(
      words.slice(0, -2).map((_, i) => words.slice(i, i + 3).join(" ")),
    );
  };
  const x = grams(a),
    y = grams(b);
  const intersection = [...x].filter((g) => y.has(g)).length;
  return intersection / Math.max(1, Math.min(x.size, y.size)) >= 0.65;
}
export function replyReview(
  context,
  observation,
  packet,
  opportunity,
  product,
  now,
  priorTexts = [],
) {
  const question = verifyContext(
    context,
    observation,
    packet,
    now,
    opportunity.question,
  );
  if (
    context.replies.length !== (opportunity.context.replies || []).length ||
    context.replies.some(
      (m) =>
        !(opportunity.context.replies || []).some(
          (old) => old.id === m.id && old.text === m.text,
        ),
    )
  )
    deny("PreparedContextChangedNeedsReview");
  if (
    packet.review.question_unresolved !== true ||
    packet.review.claims_verified !== true ||
    packet.review.language_matches !== true
  )
    deny("PreparedReplyNotQualified");
  if (priorTexts.some((text) => similarReply(text, packet.text)))
    deny("DuplicatePreparedReply");
  if (
    packet.review.requires_ai_disclosure &&
    packet.review.ai_disclosure_included !== true
  )
    deny("AiDisclosureRequired");
  const sources = sourceMap(observation, context);
  const evidence = [
    "question_unresolved",
    "claims_verified",
    "language_matches",
  ].flatMap((key) => cite(packet, key, sources));
  const links = packet.text.match(/https?:\/\/[^\s<>]+/g) || [];
  const mentions =
    /ai\s*gram|а[ий]грам|org\.telegram\.messenger\.aigram/iu.test(packet.text);
  if (mentions || links.length) {
    if (
      product?.recommendations_enabled !== true ||
      !product.target_build_version ||
      !Array.isArray(product.build_acceptance_records) ||
      !product.build_acceptance_records.length ||
      packet.review.support_only ||
      packet.review.recommendation_allowed !== true ||
      packet.review.relationship_disclosed !== true
    )
      deny("ProductRecommendationNotAccepted");
    evidence.push(...cite(packet, "recommendation_allowed", sources));
  }
  if (
    links.length > 1 ||
    (links.length &&
      (packet.review.links_allowed !== true ||
        product?.links_enabled !== true ||
        links[0] !== product.official_store_url))
  )
    deny("PreparedLinkNotAllowed");
  if (links.length) evidence.push(...cite(packet, "links_allowed", sources));
  return {
    verdict: "pass",
    text: packet.text,
    question,
    reviewed_at: new Date(now * 1000).toISOString(),
    evidence,
    facts: {
      automation_allowed: true,
      question_unresolved: true,
      context_read: true,
      language_matches: true,
      nonduplicate: true,
      claims_verified: true,
      discussion_continuing: packet.review.discussion_continuing === true,
      relationship_disclosed: packet.review.relationship_disclosed === true,
      recommendation_allowed: packet.review.recommendation_allowed === true,
      links_allowed: packet.review.links_allowed === true,
    },
  };
}
