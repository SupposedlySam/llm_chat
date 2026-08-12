import '../schemas/memberships.dart';
import 'package:zonai_schema/zonai_schema.dart';

MembershipRowRules main() => MembershipRowRules();

/// See the table rules for why this is open. Loopback only.
final class MembershipRowRules extends RowRules<MembershipTable, Membership> {
  MembershipRowRules() : super(memberships);

  @override
  Future<bool> canCreate(Jwt? jwt, Membership row) async => true;

  @override
  Future<bool> canView(Jwt? jwt, Membership row) async => true;

  @override
  Future<bool> canUpdate(Jwt? jwt, Membership before, Membership after) async =>
      true;

  @override
  Future<bool> canDelete(Jwt? jwt, Membership row) async => true;
}
