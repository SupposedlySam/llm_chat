import '../schemas/channels.dart';
import 'package:zonai_schema/zonai_schema.dart';

ChannelTableRules main() => ChannelTableRules();

/// **Open, because this binds to loopback only.**
///
/// There are no accounts here. An agent joins a room by saying who it is, and
/// the room takes it at its word — the same trust model as two people at one
/// desk. Adding auth would mean every agent needs a credential before it can
/// say hello, which is most of the friction this is meant to remove.
///
/// What that costs, stated plainly (INV6 — say what a guard does NOT catch):
/// anything that can reach the port can speak as any identity and read every
/// channel. That is acceptable on 127.0.0.1 and wrong the moment this listens
/// anywhere else. If it ever needs to, this file is where that decision lands,
/// and it needs a real auth table rather than a tightened rule.
///
/// Both rules files exist deliberately: a table with table rules and no row
/// rules fails at runtime with "Rules exist for X but there are no row-level
/// rules". Scaffold the pair together, always.
final class ChannelTableRules extends TableRules<ChannelTable, Channel> {
  ChannelTableRules() : super(channels);

  @override
  Future<bool> canCreate(Jwt? jwt) async => true;

  @override
  Future<bool> canList(Jwt? jwt) async => true;

  @override
  Future<bool> canView(Jwt? jwt) async => true;

  @override
  Future<bool> canUpdate(Jwt? jwt) async => true;

  @override
  Future<bool> canDelete(Jwt? jwt) async => true;
}
