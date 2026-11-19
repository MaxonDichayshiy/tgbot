import SwiftUI

@main
struct SlopchikApp: App {
    @AppStorage("slopchik.authToken") private var authToken = ""
    @AppStorage("slopchik.userName") private var userName = ""
    @AppStorage("slopchik.userID") private var userID = ""
    @AppStorage("slopchik.theme") private var themeRaw = SlopThemeMode.system.rawValue

    var body: some Scene {
        WindowGroup {
            Group {
                if authToken.isEmpty {
                    LoginView { token, name, id in
                        authToken = token
                        userName = name
                        userID = id
                    }
                } else {
                    ChatView(token: authToken, userName: userName, userID: userID) {
                        authToken = ""
                        userName = ""
                    }
                }
            }
            .preferredColorScheme(SlopThemeMode(rawValue: themeRaw)?.colorScheme)
        }
    }
}
