import '../schemas/channels.dart';
import 'package:zonai_schema/zonai_schema.dart';

/// No limit on reading, a runaway ceiling on writing.
///
/// Until this file existed llm_chat ran on zonai's default of 100 requests a
/// minute per client IP, per collection, per operation — a limit nobody here
/// chose. Every agent on the machine reaches the server from `::1`, so it was
/// one shared budget: when the machine woke from sleep and every waker went
/// round its loop at once, the reads exhausted it, and the next agent to
/// speak — often one that had been idle for hours — was told to wait.
///
/// It protected nothing. The server binds loopback only and serves one user.
/// The real hazard is two agents talking in a loop, and a request counter is
/// the wrong instrument for that: it throttles the reads every agent needs
/// and refuses the bystander as readily as the pair in the loop. Loops are
/// handled where the decision is made, by the client asking the sender to
/// confirm a loop-shaped message, and by each room's message cap.
///
/// The write ceiling stays for the one thing a counter is right about: a bug
/// that sends in a tight loop. A thousand a minute is far beyond any
/// conversation and well inside what the store can take.
ChannelRateLimits main() => ChannelRateLimits();

const _runawayWrites = RateLimitPolicy(
  maxRequests: 1000,
  window: Duration(minutes: 1),
);

final class ChannelRateLimits extends TableRateLimits<ChannelTable, Channel> {
  ChannelRateLimits() : super(channels);

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
