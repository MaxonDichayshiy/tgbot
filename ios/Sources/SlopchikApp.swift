import SwiftUI

@main
struct SlopchikApp: App {
    @AppStorage("slopchik.authToken") private var authToken = ""
    @AppStorage("slopchik.userName") private var userName = ""
    @AppStorage("slopchik.theme") private var themeRaw = SlopThemeMode.system.rawValue

    var body: some Scene {
        WindowGroup {
            Group {
                if authToken.isEmpty {
                    LoginView { token, name in
                        authToken = token
                        userName = name
                    }
                } else {
                    ChatView(token: authToken, userName: userName) {
                        authToken = ""
                        userName = ""
                    }
                }
            }
            .preferredColorScheme(SlopThemeMode(rawValue: themeRaw)?.colorScheme)
        }
    }
}
