import '../schemas/messages.dart';
import 'package:zonai_schema/zonai_schema.dart';

/// No limit on reading, a runaway ceiling on writing. The reasoning is in
/// channels_rate_limits.dart. A conversation loop is not stopped here: each
/// room has a message cap.
MessageRateLimits main() => MessageRateLimits();

const _runawayWrites = RateLimitPolicy(
  maxRequests: 1000,
  window: Duration(minutes: 1),
);

final class MessageRateLimits extends TableRateLimits<MessageTable, Message> {
  MessageRateLimits() : super(messages);

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
