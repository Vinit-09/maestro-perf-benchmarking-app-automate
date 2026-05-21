import SwiftUI

struct ContentView: View {
    @State private var query: String = ""

    private let items: [String] = [
        "BrowserStack", "Apple", "Google", "Microsoft", "Amazon",
        "Meta", "Netflix", "Tesla", "Nvidia", "Intel",
        "Oracle", "Salesforce", "Adobe", "IBM", "Spotify",
        "Snapchat", "TikTok", "Twitter", "LinkedIn", "Slack",
        "Notion", "Figma", "Stripe", "Shopify", "Airbnb",
        "Uber", "Lyft", "DoorDash", "Pinterest", "Reddit"
    ]

    var filtered: [String] {
        guard !query.isEmpty else { return items }
        return items.filter { $0.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                TextField("Search companies", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled(true)
                    .textInputAutocapitalization(.never)
                    .padding(.horizontal)
                    .accessibilityIdentifier("searchField")

                List(filtered, id: \.self) { name in
                    Text(name)
                        .accessibilityIdentifier("item_\(name)")
                }
                .accessibilityIdentifier("resultsList")
            }
            .navigationTitle("HelloBench")
        }
    }
}

#Preview {
    ContentView()
}
