# Homebrew formula STUB for okto-neuron.
#
# This is a TEMPLATE that documents the intended Homebrew shape; it is NOT
# yet published to a tap and the main okto-neuron package does NOT trigger
# any Homebrew install on its own. To use it locally as a tap-from-source:
#
#   brew install --build-from-source ./packaging/brew/okto-neuron.rb
#
# After install, the user explicitly opts in to the background service:
#
#   brew services start okto-neuron        # foreground via brew's launchd wrapper
#   brew services stop  okto-neuron
#   brew services info  okto-neuron
#
# The `service` block below registers ONLY when the user runs
# `brew services start`. Plain `brew install` never spawns it.
#
# Replace `url`, `sha256`, and `version` when cutting a real release.

class OktoNeuron < Formula
  include Language::Python::Virtualenv

  desc     "Okto Neuron: local-first knowledge graph memory for agents"
  homepage "https://github.com/OktoLabsAI/okto-neuron"
  url      "https://github.com/OktoLabsAI/okto-neuron/archive/refs/tags/v0.0.0.tar.gz"
  sha256   "0000000000000000000000000000000000000000000000000000000000000000"
  license  "Elastic-2.0"
  version  "0.0.0"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  # OPT-IN background service. Only activates on `brew services start okto-neuron`.
  # Vault selection is browser/client scoped, not a service startup argument.
  service do
    run [
      opt_bin/"okto-neuron",
      "serve",
      "--foreground",
      "--no-open",
    ]
    keep_alive  true
    run_type    :immediate
    working_dir Dir.home
    log_path    var/"log/okto-neuron/okto-neuron.out.log"
    error_log_path var/"log/okto-neuron/okto-neuron.err.log"
    environment_variables PATH: std_service_path_env
  end

  test do
    assert_match "Okto Neuron", shell_output("#{bin}/okto-neuron --help")
  end
end
