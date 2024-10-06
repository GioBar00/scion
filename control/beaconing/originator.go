// Copyright 2019 Anapaya Systems
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package beaconing

import (
	"context"
	"crypto/rand"
	"math/big"
	"net"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/scionproto/scion/control/ifstate"
	"github.com/scionproto/scion/pkg/addr"
	"github.com/scionproto/scion/pkg/log"
	"github.com/scionproto/scion/pkg/metrics"
	"github.com/scionproto/scion/pkg/private/prom"
	"github.com/scionproto/scion/pkg/private/serrors"
	seg "github.com/scionproto/scion/pkg/segment"
	"github.com/scionproto/scion/private/periodic"
	"github.com/scionproto/scion/private/procperf"
)

var _ periodic.Task = (*Originator)(nil)

// SenderFactory can be used to create a new beacon sender.
type SenderFactory interface {
	// NewSender creates a new beacon sender to the specified ISD-AS over the given egress
	// interface. Nexthop is the internal router endpoint that owns the egress interface. The caller
	// is required to close the sender once it's not used anymore.
	NewSender(
		ctx context.Context,
		dst addr.IA,
		egress uint16,
		nexthop *net.UDPAddr,
	) (Sender, error)
}

// Sender sends beacons on an established connection.
type Sender interface {
	// Send sends the beacon on an established connection
	Send(ctx context.Context, b *seg.PathSegment) error
	// Close closes the resources associated with the sender. It must be invoked to avoid leaking
	// connections.
	Close() error
}

// Originator originates beacons. It should only be used by core ASes.
type Originator struct {
	Extender              Extender
	SenderFactory         SenderFactory
	IA                    addr.IA
	Signer                seg.Signer
	AllInterfaces         *ifstate.Interfaces
	OriginationInterfaces func() []*ifstate.Interface

	Originated metrics.Counter

	// Tick is mutable.
	Tick Tick
}

// Name returns the tasks name.
func (o *Originator) Name() string {
	return "control_beaconing_originator"
}

// Run originates core and downstream beacons.
func (o *Originator) Run(ctx context.Context) {
	o.Tick.SetNow(time.Now())
	o.originateBeacons(ctx)
	o.Tick.UpdateLast()
}

// originateBeacons creates and sends a beacon for each active interface.
func (o *Originator) originateBeacons(ctx context.Context) {
	intfs := o.needBeacon(o.OriginationInterfaces())
	sort.Slice(intfs, func(i, j int) bool {
		return intfs[i].TopoInfo().ID < intfs[j].TopoInfo().ID
	})
	if len(intfs) == 0 {
		return
	}

	// Only log on info and error level every propagation period to reduce
	// noise. The offending logs events are redirected to debug level.
	silent := !o.Tick.Passed()
	logger := withSilent(ctx, silent)

	s := newSummary()
	var wg sync.WaitGroup
	wg.Add(len(intfs))
	for _, intf := range intfs {
		b := beaconOriginator{
			Originator: o,
			intf:       intf,
			timestamp:  o.Tick.Now(),
			summary:    s,
		}
		go func() {
			defer log.HandlePanic()
			defer wg.Done()

			if err := b.originateBeacon(ctx); err != nil {
				logger.Info("Unable to originate on interface",
					"egress_interface", b.intf.TopoInfo().ID, "err", err)
			}
		}()
	}
	wg.Wait()
	o.logSummary(logger, s)
}

// needBeacon returns a list of interfaces that need a beacon.
func (o *Originator) needBeacon(active []*ifstate.Interface) []*ifstate.Interface {
	if o.Tick.Passed() {
		return active
	}
	var stale []*ifstate.Interface
	for _, intf := range active {
		if o.Tick.Overdue(intf.LastOriginate()) {
			stale = append(stale, intf)
		}
	}
	return stale
}

func (o *Originator) logSummary(logger log.Logger, s *summary) {
	if o.Tick.Passed() {
		logger.Debug("Originated beacons", "egress_interfaces", s.IfIDs())
		return
	}
	if s.count > 0 {
		logger.Debug("Originated beacons on stale interfaces", "egress_interfaces", s.IfIDs())
	}
}

// beaconOriginator originates one beacon on the given interface.
type beaconOriginator struct {
	*Originator
	intf      *ifstate.Interface
	timestamp time.Time
	summary   *summary
}

// originateBeacon originates a beacon on the given ifID.
func (o *beaconOriginator) originateBeacon(ctx context.Context) error {
	pp := procperf.GetNew(procperf.Originated, "") // Add beacon ID after creation
	timeCreateS := time.Now()
	labels := originatorLabels{intf: o.intf}
	topoInfo := o.intf.TopoInfo()
	beacon, err := o.createBeacon(ctx)
	if err != nil {
		o.incrementMetrics(labels.WithResult("err_create"))
		return serrors.Wrap("creating beacon", err, "egress_interface", o.intf.TopoInfo().ID)
	}
	timeCreateE := time.Now()
	pp.AddDurationT(timeCreateS, timeCreateE) // 0
	bcnId := procperf.GetFullId(beacon.GetLoggingID(), beacon.Info.SegmentID)
	pp.SetID(bcnId)
	pp.SetNextID(bcnId)
	defer pp.Write()
	timeSenderS := time.Now()
	senderCtx, cancelF := context.WithTimeout(ctx, defaultNewSenderTimeout)
	defer cancelF()

	sender, err := o.SenderFactory.NewSender(
		senderCtx,
		topoInfo.IA,
		o.intf.TopoInfo().ID,
		net.UDPAddrFromAddrPort(topoInfo.InternalAddr),
	)
	if err != nil {
		o.incrementMetrics(labels.WithResult(prom.ErrNetwork))
		return serrors.Wrap("getting beacon sender", err,
			"waited_for", time.Since(timeSenderS).String())

	}
	defer sender.Close()
	timeSenderE := time.Now()
	pp.AddDurationT(timeSenderS, timeSenderE) // 1
	timeSendS := time.Now()
	if err := sender.Send(ctx, beacon); err != nil {
		o.incrementMetrics(labels.WithResult(prom.ErrNetwork))
		return serrors.Wrap("sending beacon", err,
			"waited_for", time.Since(timeSendS).String())

	}
	timeSendE := time.Now()
	pp.AddDurationT(timeSendS, timeSendE) // 2
	timeMetricsS := time.Now()
	o.onSuccess(o.intf)
	o.incrementMetrics(labels.WithResult(prom.Success))
	timeMetricsE := time.Now()
	pp.AddDurationT(timeMetricsS, timeMetricsE) // 3

	return nil
}

func (o *beaconOriginator) createBeacon(ctx context.Context) (*seg.PathSegment, error) {
	segID, err := rand.Int(rand.Reader, big.NewInt(1<<16))
	if err != nil {
		return nil, err
	}
	beacon, err := seg.CreateSegment(o.timestamp, uint16(segID.Uint64()))
	if err != nil {
		return nil, serrors.Wrap("creating segment", err)
	}

	if err := o.Extender.Extend(ctx, beacon, 0, o.intf.TopoInfo().ID, nil); err != nil {
		return nil, serrors.Wrap("extending segment", err)
	}
	return beacon, nil
}

func (o *beaconOriginator) onSuccess(intf *ifstate.Interface) {
	intf.Originate(o.Tick.Now())
	o.summary.AddIfID(o.intf.TopoInfo().ID)
	o.summary.Inc()
}

func (o *beaconOriginator) incrementMetrics(labels originatorLabels) {
	if o.Originator.Originated == nil {
		return
	}
	o.Originator.Originated.With(labels.Expand()...).Add(1)
}

type originatorLabels struct {
	intf   *ifstate.Interface
	Result string
}

func (l originatorLabels) Expand() []string {
	return []string{"egress_interface", strconv.Itoa(int(l.intf.TopoInfo().ID)),
		prom.LabelResult, l.Result}
}

func (l originatorLabels) WithResult(result string) originatorLabels {
	l.Result = result
	return l
}
