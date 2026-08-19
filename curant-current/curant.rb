# curant.rb
# Homebrew formula for the Curant CLI client.
#
# This lives in a "tap" repo, e.g. github.com/curant-app/homebrew-curant,
# so customers install with:
#
#   brew tap curant-app/curant
#   brew install curant
#   curant-cli activate YOUR-LICENSE-KEY
#
# NOTE: url and sha256 below are placeholders. Once the real repo exists,
# replace with the actual release tarball URL and its sha256 checksum
# (run `shasum -a 256 <tarball>` after cutting a release).

class Curant < Formula
  desc "Curant — your AI Secretary, reachable by call or text"
  homepage "https://curant.app"
  url "https://github.com/curant-app/curant-cli/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_REAL_SHA256_AFTER_FIRST_RELEASE"
  license "PROPRIETARY"  # see note below — this is now correct, not just a placeholder

  depends_on "python@3.12"

  def install
    bin.install "curant-cli"
    # anthropic and openai are both installed so either provider works
    # out of the box — curant-cli only imports whichever one is actually
    # configured (see set-provider), so having both present costs nothing
    # at runtime, just a slightly bigger install. cryptography backs the
    # local backup/context-export encryption. mcp backs the Model Context
    # Protocol client — only actually used if the customer connects an
    # MCP server (curant-cli mcp-add), but needs to be present for that
    # to work at all.
    system Formula["python@3.12"].opt_bin/"pip3", "install", "anthropic", "openai", "google-genai", "cryptography", "mcp", "--quiet"
    # Future: also install the watcher/launchd service files here, e.g.
    # (prefix/"homebrew.mxcl.curant.plist").write launchd_plist_contents
  end

  def caveats
    <<~EOS
      Curant is installed but not yet activated.

      NOTE: Curant is currently a free, invite-only beta -- there is no
      public signup or payment yet. If you're seeing this, you should
      already have a license key from whoever gave you access.

      1) Connect it to your account:
           curant-cli activate YOUR-LICENSE-KEY

      2) Give it your own API key — this stays on your Mac only, it is
         never sent to Curant's server. For the beta, Gemini is the
         recommended option (Google offers a free API tier, no payment
         method needed):
           curant-cli set-provider gemini
           curant-cli set-api-key AIzaSy...

         Prefer Anthropic or OpenAI instead? Same idea — your persona,
         instructions, and memories carry over unchanged whichever you
         pick:
           curant-cli set-provider anthropic   # or: openai
           curant-cli set-api-key sk-ant-...    # or: sk-...

      Don't have a license key? This beta isn't self-serve yet — ask
      whoever pointed you here to issue one.

      Don't have an API key?
        Gemini (free tier): https://aistudio.google.com/apikey
        Anthropic:          https://console.anthropic.com/settings/keys
        OpenAI:             https://platform.openai.com/api-keys

      Curant won't respond to anything until both steps above are done —
      this install alone doesn't do anything on its own.
    EOS
  end

  test do
    system "#{bin}/curant-cli", "status"
  end
end

=begin
NOTE ON "PROPRIETARY" LICENSE ABOVE:

This used to say the opposite — that curant-cli was safe to open-source
because all real product logic (prompts, personas, memory extraction)
lived server-side. That is no longer true. As of the local-first
architecture change, curant-cli itself contains the personas, the
memory-extraction prompt, and the proactivity decision logic — that's
real product IP now sitting in this file, not just a dumb relay.

So: this tap repo should stay closed/private, or if a public repo is
still wanted for the "honest/inspectable" brand pillar, strip the
PERSONAS dict and the prompt text out into a separate mechanism before
publishing — don't publish this file as-is and call it "PROPRIETARY" in
name only. Worth deciding deliberately, not by default.
=end
