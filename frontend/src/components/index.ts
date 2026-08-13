/**
 * The shared component surface. Pages should import from here, not from the files:
 *
 *     import { Panel, GateMeters, EvidenceChip, EmptyState } from '../components';
 *
 * Everything in this barrel is stable and owned by the scaffold. If a page needs something
 * that is not here, add it here rather than growing a private variant — four pages that each
 * invent their own "confidence bar" is exactly the failure this barrel exists to prevent.
 */
export { CountryTag } from './CountryTag';
export type { CountryTagProps } from './CountryTag';

export { DocTypeBadge } from './DocTypeBadge';
export type { DocTypeBadgeProps } from './DocTypeBadge';

export { EmptyByDesign, EmptyState } from './EmptyState';
export type { EmptyStateProps } from './EmptyState';

export { AbstentionNotice, ErrorState, NeedsOcrState } from './ErrorState';
export type { ErrorStateProps } from './ErrorState';

export { EvidenceChip, EvidenceList } from './EvidenceChip';
export type { EvidenceChipProps } from './EvidenceChip';

export { JsonView } from './JsonView';
export type { JsonViewProps } from './JsonView';

export { GateMeters, Meter, asPercent } from './Meter';
export type { GateMetersProps, GateThresholds, MeterProps } from './Meter';

export { Badge, Fact, Loading, PageHead, Panel, Spinner } from './Panel';
export type { BadgeTone, PanelProps } from './Panel';

export { PiiValue } from './PiiValue';
export type { PiiValueProps } from './PiiValue';

export { ToastProvider, useToast } from './Toast';
export type { ToastTone } from './Toast';
