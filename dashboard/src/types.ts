// API response types — mirror app/services/analytics_service.py exactly.
// Every field here was verified to exist in production Firestore; if a field
// is nullable here it IS null in real data, so render defensively.

export interface DateRange {
  from: string;
  to: string;
}

export interface FunnelSessionEntry {
  phone: string | null;
  name: string | null;
  businessName: string | null;
  currentStep: string | null;
  startedAt: string | null;
  businessId: string | null;
}

export interface FunnelStage {
  stage: "started" | "details_collected" | "whatsapp_paired" | "completed";
  label: string;
  count: number;
  /** count ÷ started (null when nobody started in range) */
  pctOfStarted: number | null;
  /** how many left between the previous stage and this one */
  dropped: number;
  /** dropped ÷ previous stage count (null on the first stage) */
  dropPct: number | null;
  /** per-owner details for the drill-down modal; sorted most-recent first */
  sessions: FunnelSessionEntry[];
}

export interface GrowthPoint {
  date: string; // YYYY-MM-DD
  newBusinesses: number;
}

export interface ActivityPoint {
  date: string; // YYYY-MM-DD
  conversations: number;
  bookings: number;
}

export interface Aggregate {
  totalBusinesses: number;
  newBusinessesInRange: number;
  whatsappPaired: number;
  totalConversations: number;
  totalBookings: number;
  bookingsByStatus: Record<string, number>;
  conversationOutcomes: Record<string, number>;
  bookingConversion: number | null;
  avgCsat: number | null;
  csatResponses: number;
  openComplaints: number;
  pendingKnowledgeGaps: number;
}

export interface Account {
  id: string;
  name: string;
  ownerName: string | null;
  ownerPhone: string | null;
  businessType: string | null;
  plan: string | null;
  status: string | null;
  primaryLanguage: string | null;
  createdAt: string | null;
  trialEndsAt: string | null;
  trialDaysLeft: number | null;
  whatsappPaired: boolean;
  waPhoneNumber: string | null;
  isTest: boolean;
  bookingsInRange: number;
  conversationsInRange: number;
  openComplaints: number;
  pendingKnowledgeGaps: number;
  lastCustomerActivityAt: string | null;
  lastOwnerActivityAt: string | null;
}

export interface Overview {
  range: DateRange;
  includeTest: boolean;
  funnel: FunnelStage[];
  activeOnboardingSessions: number;
  /** demo/booking-roleplay sessions excluded from the funnel */
  excludedDemoSessions: number;
  growth: GrowthPoint[];
  activity: ActivityPoint[];
  aggregate: Aggregate;
  accounts: Account[];
}

export interface ServiceItem {
  name: string | null;
  price: number | string | null;
  duration: number | string | null;
}

export interface DetailProfile extends Omit<
  Account,
  | "bookingsInRange"
  | "conversationsInRange"
  | "openComplaints"
  | "pendingKnowledgeGaps"
  | "lastCustomerActivityAt"
> {
  lastOwnerActivityAt: string | null;
  onboardingStep: string | null;
  address: string | null;
  timezone: string | null;
  services: ServiceItem[];
  hours: string[] | null;
  openingDays: string[] | null;
  automations: Record<string, boolean> | null;
}

export interface Booking {
  id: string;
  businessId: string;
  customerName: string | null;
  customerPhone: string | null;
  service: string | null;
  partySize: number | null;
  datetime: string | null;
  status: string;
  source: string | null;
  notes: string | null;
  createdAt: string | null;
}

export interface Conversation {
  id: string;
  businessId: string;
  channel: string;
  customerPhone: string | null;
  customerName: string | null;
  startedAt: string | null;
  endedAt: string | null;
  durationSeconds: number | null;
  status: string | null;
  outcome: string | null;
  summary: string | null;
  bookingId: string | null;
  messageCount: number | null;
  live: boolean;
  aiPaused?: boolean;
}

export interface TranscriptTurn {
  role: string;
  text: string;
  ts: string | null;
}

export interface CsatPoint {
  rating: number;
  at: string | null;
  customerPhone: string | null;
}

export interface Complaint {
  id: string;
  businessId: string;
  text: string;
  type: string | null;
  customerPhone: string | null;
  customerName: string | null;
  bookingId: string | null;
  status: string;
  createdAt: string | null;
}

export interface KnowledgeGap {
  id: string;
  businessId: string;
  shortCode: string | null;
  question: string;
  answer: string | null;
  status: string;
  useCount: number;
  createdAt: string | null;
  confirmedAt: string | null;
}

export interface DetailSummary {
  bookingsInRange: number;
  bookingsTruncated: boolean;
  bookingsByStatus: Record<string, number>;
  conversationsInRange: number;
  conversationsTruncated: boolean;
  conversationOutcomes: Record<string, number>;
  bookingConversion: number | null;
  avgCsat: number | null;
  csatResponses: number;
  openComplaints: number;
  pendingKnowledgeGaps: number;
  totalCustomers: number;
}

export interface AllTimeTotals {
  bookings: number;
  conversations: number;
  lastBookingAt: string | null;
  lastConversationAt: string | null;
}

export interface BusinessDetail {
  range: DateRange;
  profile: DetailProfile;
  activity: ActivityPoint[];
  allTime: AllTimeTotals;
  summary: DetailSummary;
  bookings: Booking[];
  conversations: Conversation[];
  transcripts: Record<string, TranscriptTurn[]>;
  csatTrend: CsatPoint[];
  complaints: Complaint[];
  knowledgeGaps: KnowledgeGap[];
  knowledgeByStatus: Record<string, number>;
}

export type RangePreset = 7 | 30 | 90 | 365;

export interface GlobalKb {
  content: string;
  source: "firestore" | "default";
  updatedAt: string | null;
  chars: number;
  approxTokens: number;
  maxChars: number;
}

export interface RangeFilter {
  preset: RangePreset | null;
  from: string | null; // YYYY-MM-DD when custom
  to: string | null;
}
