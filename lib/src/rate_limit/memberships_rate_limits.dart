import '../schemas/memberships.dart';
import 'package:zonai_schema/zonai_schema.dart';

/// No limit on reading, a runaway ceiling on writing. The reasoning is in
/// channels_rate_limits.dart; this table carries the same traffic shape —
/// every `say`, `read` and waker pass reads it.
MembershipRateLimits main() => MembershipRateLimits();

const _runawayWrites = RateLimitPolicy(
  maxRequests: 1000,
  window: Duration(minutes: 1),
);

final class MembershipRateLimits
    extends TableRateLimits<MembershipTable, Membership> {
  MembershipRateLimits() : super(memberships);

  @override
  Future<RateLimitPolicy?> getPolicy() async => null;

  @override
  Future<RateLimitPolicy?> limitPolicy() async => null;

  @override
  Future<RateLimitPolicy?> countPolicy() async => null;

  @override
  Future<RateLimitPolicy?> createPolicy() async => _runawayWrites;

  @override
  Future<RateLimitPolicy?> updatePolicy() async => _runawayWrites;

  @override
  Future<RateLimitPolicy?> deletePolicy() async => _runawayWrites;
}
