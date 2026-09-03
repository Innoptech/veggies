# tflint 0.64.x - the "terraform" ruleset is bundled, no `tflint --init` needed.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
