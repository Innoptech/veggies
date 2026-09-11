output "files_branch" {
  description = "Delivery branch carrying the workflow files (merge its PR by hand); empty when manage_files is false."
  value       = var.manage_files ? var.files_branch : ""
}
