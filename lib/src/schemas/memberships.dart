import '../ids.dart';
import 'package:zonai_schema/zonai_schema.dart';

/// One agent's place in one channel.
///
/// [seenSeq] is why this table exists rather than a members list on the
/// channel. Delivery has to be **exactly once per identity**: the hook that
/// injects messages into a running session fires after every tool call, and a
/// participant that re-read its inbox would keep answering the same message.
/// A cursor per member is the only thing that makes that safe.
final class Membership {
  Membership({
    required this.id,
    required this.channel,
    required this.identity,
    required this.seenSeq,
    required this.done,
    required this.createdAt,
    this.updatedAt,
  });

  final MembershipsId id;

  /// The channel *name*, not its id — every client operation starts from the
  /// name a human typed, and a lookup-then-lookup for the common path buys
  /// nothing here.
  final String channel;

  /// Who this agent is in the room. Chosen by the human at join time
  /// ("your identity should be reviewer"), never generated: the point is that
  /// the other side can tell who is talking.
  final String identity;

  /// The highest message `seq` this member has been shown.
  final int seenSeq;

  /// They have said their piece. When every member is done, the room closes.
  final bool done;

  final DateTime createdAt;
  final DateTime? updatedAt;
}

final class MembershipTable extends Table<Membership> {
  MembershipTable(super.$)
    : id = $.id('id', (s) => s.id,
          fromString: MembershipsId.new, generate: MembershipsId.generate),
      channel = $.text('channel', (s) => s.channel),
      identity = $.text('identity', (s) => s.identity),
      seenSeq = $.integer('seen_seq', (s) => s.seenSeq),
      done = $.boolean('done', (s) => s.done),
      createdAt = $.createdAt('created_at', (s) => s.createdAt),
      updatedAt = $.updatedAt('updated_at', (s) => s.updatedAt);

  @override
  Membership fromRow(RowReader read) => Membership(
    id: read(id),
    channel: read(channel),
    identity: read(identity),
    seenSeq: read(seenSeq),
    done: read(done),
    createdAt: read(createdAt),
    updatedAt: read(updatedAt),
  );

  final IdColumn<MembershipsId> id;
  final TextColumn channel;
  final TextColumn identity;
  final ColumnType<int> seenSeq;
  final ColumnType<bool> done;
  final DateTimeColumn createdAt;
  final ColumnType<DateTime?> updatedAt;
}

final memberships = table('memberships', MembershipTable.new);
