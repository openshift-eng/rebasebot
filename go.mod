module github.com/shiftstack/merge-bot

go 1.16

replace k8s.io/client-go => k8s.io/client-go v0.21.1

require (
	github.com/sirupsen/logrus v1.8.1
	k8s.io/test-infra v0.0.0-20210602061843-6c4aa46d13da
	sigs.k8s.io/controller-runtime v0.9.0-beta.5 // indirect
)
