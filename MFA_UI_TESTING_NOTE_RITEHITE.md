# RiteHite UI Testing MFA Note

Context: Internal UI-login testing automation for the RiteHite org.

## Confirmed state for the checked admin user

- User has assigned permission set: `Waive_MFA_For_UI_Logins_RiteHite`
- That permission set includes: `PermissionsBypassMFAForUiLogins = true`

## Confirmed profile session selection in UI

- Profile: `System Administrator`
- Session setting selected: `Session Security Level Required at Login = --None--`

## Additional checked signals (same user/org review)

- Profile does not force MFA (`PermissionsForceTwoFactor = false`)
- No assigned permission set with `PermissionsForceTwoFactor = true`

## Note

This file is a persistent project note for the current test-use configuration and decisions.
