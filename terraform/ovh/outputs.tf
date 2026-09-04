output "instance_id" {
  description = "OpenStack server ID."
  value       = openstack_compute_instance_v2.veggies.id
}

output "public_ip" {
  description = "Public IPv4 for the bootstrap window (closed afterwards)."
  value       = openstack_compute_instance_v2.veggies.access_ip_v4
}

output "data_volume_id" {
  description = "Block storage volume attached for /srv and runner work dirs."
  value       = openstack_blockstorage_volume_v3.data.id
}
